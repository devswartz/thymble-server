"""Connector accounts (architecture §6.2). Credentials are write-only: stored encrypted,
never returned. The worker picks up changes within its supervisor interval."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select

from thymble.api.deps import DbSession, Household
from thymble.api.shopping import service
from thymble.api.shopping_schemas import ConnectorIn, ConnectorOut, ConnectorPatch
from thymble.config import Settings, get_settings
from thymble.connectors.crypto import CredentialsUnavailable, encrypt_credentials
from thymble.connectors.home_assistant.client import websocket_url
from thymble.connectors.home_assistant.engine import KIND as HA_KIND
from thymble.connectors.home_assistant.engine import EngineOptions
from thymble.models import ConnectorAccount, ConnectorItemLink, Store
from thymble.shopping.errors import ShoppingError
from thymble.shopping.service import utcnow

router = APIRouter(prefix="/connectors", tags=["connectors"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _out(account: ConnectorAccount) -> ConnectorOut:
    return ConnectorOut(
        id=account.id,
        kind=account.kind,
        display_name=account.display_name,
        base_url=account.base_url,
        config=account.config,
        status=account.status,
        has_credentials=bool(account.credentials_encrypted),
        last_synced_at=account.last_synced_at,
        last_error=account.last_error,
    )


def _get(db, hh, account_id: uuid.UUID) -> ConnectorAccount:
    account = db.get(ConnectorAccount, account_id)
    if account is None or account.household_id != hh.household_id or account.deleted_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connector account not found")
    return account


def _validate(svc, kind: str, base_url: str | None, config: dict[str, Any]) -> None:
    if kind != HA_KIND:
        raise ShoppingError(f"unknown list connector {kind!r}")
    try:
        websocket_url(base_url or "")
        options = EngineOptions.from_config(config)
    except ValueError as exc:
        raise ShoppingError(str(exc)) from exc
    if options.list_id is not None:
        svc.get_list(options.list_id)


def _encrypt(settings: Settings, token: str) -> bytes:
    try:
        return encrypt_credentials(settings, {"token": token})
    except CredentialsUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "connector credentials can't be stored: " + str(exc),
        ) from exc


@router.get("")
def list_connectors(db: DbSession, hh: Household) -> list[ConnectorOut]:
    rows = db.scalars(
        select(ConnectorAccount)
        .where(
            ConnectorAccount.household_id == hh.household_id,
            ConnectorAccount.deleted_at.is_(None),
        )
        .order_by(ConnectorAccount.created_at)
    )
    return [_out(a) for a in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_connector(
    body: ConnectorIn, db: DbSession, hh: Household, settings: SettingsDep
) -> ConnectorOut:
    """Link a Home Assistant instance: base_url, a long-lived access token, and config
    `{"main_entity": "todo.shopping_list", "list_id"?, "dry_run"?, "grace_period_hours"?}`.
    Map stores to other todo lists with PATCH /v1/stores/{id} (external_list_*)."""
    if body.kind not in settings.connectors_lists:
        raise ShoppingError(f"connector {body.kind!r} is not enabled on this server")
    svc = service(db, hh)
    _validate(svc, body.kind, body.base_url, body.config)
    if not body.token:
        raise ShoppingError("a Home Assistant long-lived access token is required")
    account = ConnectorAccount(
        household_id=hh.household_id,
        kind=body.kind,
        display_name=body.display_name,
        base_url=body.base_url,
        config=body.config,
        status="active",
        credentials_encrypted=_encrypt(settings, body.token),
    )
    db.add(account)
    db.commit()
    return _out(account)


@router.get("/{account_id}")
def get_connector(account_id: uuid.UUID, db: DbSession, hh: Household) -> ConnectorOut:
    return _out(_get(db, hh, account_id))


@router.patch("/{account_id}")
def update_connector(
    account_id: uuid.UUID, body: ConnectorPatch, db: DbSession, hh: Household, settings: SettingsDep
) -> ConnectorOut:
    account = _get(db, hh, account_id)
    base_url = body.base_url if body.base_url is not None else account.base_url
    config = body.config if body.config is not None else (account.config or {})
    _validate(service(db, hh), account.kind, base_url, config)
    account.base_url, account.config = base_url, config
    if body.display_name is not None:
        account.display_name = body.display_name
    if body.status is not None:
        account.status = body.status
    if body.token:
        account.credentials_encrypted = _encrypt(settings, body.token)
        account.last_error = None
    db.commit()
    return _out(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector(account_id: uuid.UUID, db: DbSession, hh: Household) -> None:
    """Unlink. Items stay in Thymble; the external list is left as it is."""
    account = _get(db, hh, account_id)
    account.deleted_at = utcnow()
    account.credentials_encrypted = None
    for store in db.scalars(
        select(Store).where(Store.external_list_connector_account_id == account.id)
    ):
        store.external_list_connector_account_id = None
        store.external_list_id = None
    db.execute(
        delete(ConnectorItemLink).where(ConnectorItemLink.connector_account_id == account.id)
    )
    db.commit()
