"""Home Assistant WebSocket client: only what the list connector needs.

Protocol (https://developers.home-assistant.io/docs/api/websocket): `auth_required` ->
`auth {access_token}` -> `auth_ok`, then `{"id", "type", ...}` commands answered by
`{"id", "type": "result", "success", "result" | "error"}`; subscriptions push
`{"id", "type": "event", "event"}`.

`HAClient` talks to any transport with `async send(text)`, `async recv() -> text` and
`async close()` that raises `ConnectionClosed` once the socket is gone.
`WebSocketTransport` is the real one; the test suite uses an in-memory fake HA.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("thymble.connectors.home_assistant")


class ConnectionClosed(Exception):
    """The websocket is gone (or could not be opened)."""


class MaybeSent(ConnectionClosed):
    """The request went out but the socket died before its result arrived."""


class HAError(Exception):
    """HA answered with an error (bad auth, unknown entity, failed service call)."""


class AuthError(HAError):
    """The access token was rejected; retrying won't help until it is replaced."""


class Transport(Protocol):
    async def send(self, text: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


def websocket_url(base_url: str) -> str:
    """http(s)://host[:port][/prefix] -> ws(s)://host[:port][/prefix]/api/websocket"""
    parts = urlsplit(base_url.strip())
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
    if scheme is None or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {base_url!r}")
    path = parts.path.rstrip("/")
    if not path.endswith("/api/websocket"):
        path += "/api/websocket"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class WebSocketTransport:
    """A real websocket to HA (the `websockets` package).

    websockets' own keepalive pings detect a silently dead socket, which then surfaces
    as ConnectionClosed and triggers the connector's reconnect.
    """

    def __init__(self, ws: Any):
        self._ws = ws

    @classmethod
    async def open(cls, base_url: str, *, timeout: float = 15.0) -> WebSocketTransport:
        import websockets

        try:
            ws = await websockets.connect(
                websocket_url(base_url),
                open_timeout=timeout,
                ping_interval=20,
                ping_timeout=20,
                max_size=16 * 1024 * 1024,
            )
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            raise ConnectionClosed(f"could not connect: {exc!r}") from exc
        return cls(ws)

    async def send(self, text: str) -> None:
        import websockets

        try:
            await self._ws.send(text)
        except websockets.ConnectionClosed as exc:
            raise ConnectionClosed() from exc

    async def recv(self) -> str:
        import websockets

        try:
            msg = await self._ws.recv()
        except websockets.ConnectionClosed as exc:
            raise ConnectionClosed() from exc
        return msg if isinstance(msg, str) else msg.decode()

    async def close(self) -> None:
        await self._ws.close()


class HAClient:
    def __init__(self, transport: Transport, token: str):
        self._t = transport
        self._token = token
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._subs: dict[int, Callable[[dict], None]] = {}
        self.closed = asyncio.Event()
        self.ha_version: str | None = None
        self._reader: asyncio.Task | None = None

    async def start(self) -> None:
        msg = json.loads(await self._t.recv())
        if msg.get("type") != "auth_required":
            raise HAError(f"unexpected greeting {msg.get('type')!r}")
        await self._t.send(json.dumps({"type": "auth", "access_token": self._token}))
        msg = json.loads(await self._t.recv())
        if msg.get("type") == "auth_invalid":
            raise AuthError(f"auth failed: {msg.get('message')}")
        if msg.get("type") != "auth_ok":
            raise HAError(f"unexpected auth reply {msg.get('type')!r}")
        self.ha_version = msg.get("ha_version")
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        await self._t.close()
        if self._reader:
            await asyncio.gather(self._reader, return_exceptions=True)

    def _dispatch(self, msg: dict) -> None:
        if msg.get("type") == "result":
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif msg.get("type") == "event":
            cb = self._subs.get(msg.get("id"))
            if cb:
                cb(msg["event"])

    async def _read(self) -> None:
        try:
            while True:
                data = json.loads(await self._t.recv())
                # HA may coalesce several messages into one JSON array.
                for msg in data if isinstance(data, list) else [data]:
                    self._dispatch(msg)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("HA websocket reader failed")
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionClosed())
            self._pending.clear()

    async def _request(self, payload: dict, on_event: Callable[[dict], None] | None = None) -> Any:
        if self.closed.is_set():
            raise ConnectionClosed()
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        if on_event:
            self._subs[mid] = on_event
        try:
            await self._t.send(json.dumps({"id": mid, **payload}))
        except ConnectionClosed:
            self._pending.pop(mid, None)
            self._subs.pop(mid, None)
            raise  # definitely not sent
        try:
            msg = await fut
        except ConnectionClosed:
            raise MaybeSent() from None  # sent, but we never saw the result
        if not msg.get("success"):
            self._subs.pop(mid, None)
            raise HAError(msg.get("error"))
        return msg.get("result")

    async def subscribe_todo(self, entity_id: str, on_event: Callable[[dict], None]) -> None:
        await self._request({"type": "todo/item/subscribe", "entity_id": entity_id}, on_event)

    async def list_items(self, entity_id: str) -> list[dict]:
        res = await self._request({"type": "todo/item/list", "entity_id": entity_id})
        return res["items"]

    async def call_service(self, service: str, entity_id: str, data: dict) -> None:
        await self._request(
            {
                "type": "call_service",
                "domain": "todo",
                "service": service,
                "service_data": data,
                "target": {"entity_id": entity_id},
            }
        )
