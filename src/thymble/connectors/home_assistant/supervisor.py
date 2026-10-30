"""Runs one HASyncEngine per active Home Assistant connector account, in the worker.

Every `interval` seconds the supervisor reloads the accounts: new ones start, deleted or
disabled ones stop, and an account whose settings or store-linked lists changed is
restarted. With several worker replicas, a Redis lease per account makes sure only one
of them runs a given connector.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from thymble.config import Settings
from thymble.connectors.crypto import CredentialsUnavailable, decrypt_credentials
from thymble.connectors.home_assistant.client import Transport, WebSocketTransport
from thymble.connectors.home_assistant.engine import KIND, EngineOptions, HASyncEngine
from thymble.models import ConnectorAccount, Store

log = logging.getLogger("thymble.connectors.home_assistant")

ConnectFactory = Callable[[ConnectorAccount], Callable[[], Awaitable[Transport]]]


def websocket_connect(account: ConnectorAccount) -> Callable[[], Awaitable[Transport]]:
    base_url = account.base_url or ""

    async def connect() -> Transport:
        return await WebSocketTransport.open(base_url)

    return connect


class ConnectorSupervisor:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        *,
        connect_factory: ConnectFactory = websocket_connect,
        redis: Any = None,
        interval: float = 30.0,
        lease_ttl: float = 90.0,
        engine_overrides: dict[str, Any] | None = None,
    ):
        self.settings = settings
        self._sessions = session_factory
        self._connect_factory = connect_factory
        self._redis = redis
        self.interval = interval
        self.lease_ttl = lease_ttl
        self._overrides = dict(engine_overrides or {})
        self._owner = uuid.uuid4().hex
        self.engines: dict[uuid.UUID, HASyncEngine] = {}
        self._fingerprints: dict[uuid.UUID, str] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for account_id in list(self.engines):
            await self._stop_engine(account_id)

    async def _loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("connector supervisor pass failed")
            await asyncio.sleep(self.interval)

    def _wanted(self) -> dict[uuid.UUID, tuple[ConnectorAccount, str]]:
        with self._sessions() as s:
            accounts = s.scalars(
                select(ConnectorAccount).where(
                    ConnectorAccount.kind == KIND,
                    ConnectorAccount.deleted_at.is_(None),
                    ConnectorAccount.status == "active",
                )
            ).all()
            wanted = {}
            for account in accounts:
                stores = s.execute(
                    select(Store.id, Store.external_list_id).where(
                        Store.external_list_connector_account_id == account.id,
                        Store.deleted_at.is_(None),
                    )
                ).all()
                fp = hashlib.sha256(
                    json.dumps(
                        [
                            account.base_url,
                            account.config,
                            account.credentials_encrypted.hex()
                            if account.credentials_encrypted
                            else None,
                            sorted((str(i), e) for i, e in stores if e),
                        ],
                        sort_keys=True,
                        default=str,
                    ).encode()
                ).hexdigest()
                s.expunge(account)
                wanted[account.id] = (account, fp)
            return wanted

    async def reconcile(self) -> None:
        wanted = self._wanted()
        for account_id in list(self.engines):
            if account_id not in wanted or wanted[account_id][1] != self._fingerprints.get(
                account_id
            ):
                await self._stop_engine(account_id)
        for account_id, (account, fp) in wanted.items():
            if account_id in self.engines:
                await self._renew_lease(account_id)
                continue
            if not await self._take_lease(account_id):
                continue
            engine = self._build(account)
            if engine is None:
                await self._release_lease(account_id)
                continue
            try:
                engine.load()
            except Exception as exc:
                log.warning("connector %s can't start: %r", account_id, exc)
                self._set_error(account_id, f"Can't start: {exc}")
                await self._release_lease(account_id)
                continue
            engine.start()
            self.engines[account_id] = engine
            self._fingerprints[account_id] = fp
            log.info(
                "started Home Assistant connector %s%s",
                account_id,
                " (dry run)" if engine.dry_run else "",
            )

    def _build(self, account: ConnectorAccount) -> HASyncEngine | None:
        try:
            creds = decrypt_credentials(self.settings, account.credentials_encrypted)
            options = EngineOptions.from_config(account.config, **self._overrides)
        except (CredentialsUnavailable, ValueError) as exc:
            log.warning("connector %s can't start: %s", account.id, exc)
            self._set_error(account.id, f"Can't start: {exc}")
            return None
        return HASyncEngine(
            account.id,
            self._sessions,
            self._connect_factory(account),
            creds.get("token", ""),
            options,
        )

    def _set_error(self, account_id: uuid.UUID, error: str) -> None:
        with self._sessions() as s:
            account = s.get(ConnectorAccount, account_id)
            if account is not None and account.last_error != error:
                account.last_error = error
                s.commit()

    async def _stop_engine(self, account_id: uuid.UUID) -> None:
        engine = self.engines.pop(account_id, None)
        self._fingerprints.pop(account_id, None)
        if engine is not None:
            await engine.stop()
            log.info("stopped Home Assistant connector %s", account_id)
        await self._release_lease(account_id)

    # ----------------------------------------------------------- redis lease
    def _lease_key(self, account_id: uuid.UUID) -> str:
        return f"thymble:connector-lease:{account_id}"

    async def _take_lease(self, account_id: uuid.UUID) -> bool:
        if self._redis is None:
            return True
        ttl = int(self.lease_ttl * 1000)
        got = await self._redis.set(self._lease_key(account_id), self._owner, nx=True, px=ttl)
        return bool(got)

    async def _renew_lease(self, account_id: uuid.UUID) -> None:
        if self._redis is None:
            return
        key = self._lease_key(account_id)
        owner = await self._redis.get(key)
        if owner in (self._owner, self._owner.encode()):
            await self._redis.pexpire(key, int(self.lease_ttl * 1000))
        else:
            log.warning("lost the lease for connector %s; stopping it", account_id)
            engine = self.engines.pop(account_id, None)
            self._fingerprints.pop(account_id, None)
            if engine is not None:
                await engine.stop()

    async def _release_lease(self, account_id: uuid.UUID) -> None:
        if self._redis is None:
            return
        key = self._lease_key(account_id)
        owner = await self._redis.get(key)
        if owner in (self._owner, self._owner.encode()):
            await self._redis.delete(key)
