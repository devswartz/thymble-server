"""In-process fake of Home Assistant's WebSocket API for `todo` entities.

Ported from the Thymble planning repo's spikes/ha-connector. Reproduces the behavior
verified against HA 2026.9.4 + `alexa_devices` (architecture §6.3):

* WebSocket protocol shape: `auth_required` -> `auth` -> `auth_ok`, then
  `{"id", "type", ...}` commands answered by `{"id", "type": "result"}`.
* `todo/item/subscribe {entity_id}` -> result, then `event` messages whose
  payload is a full snapshot `{"items": [{uid, summary, status, due,
  description, completed}, ...]}` (one immediately, one per change).
* `todo/item/list {entity_id}` -> `{"items": [...]}` (same window).
* `call_service` todo.add_item {item}, todo.update_item {item: uid|summary,
  rename?, status?}, todo.remove_item {item: uid|summary | [..]}.
  Service results carry only a `context`, never item data.
* **Every change fires the subscription twice**, `double_fire_delay` apart,
  with the *identical* (snapshot-at-change-time) payload.
* **Only the `window` most recently touched items are visible.** Older items
  drop out of snapshots and reappear when the list shrinks.
* Alexa-side changes (voice / Alexa app) land after `alexa_delay` (~25 s real,
  compress in tests). Voice duplicates get separate uids.

No sockets are opened: `FakeHA.connect()` returns an in-memory transport that
exchanges JSON *text*, so the client code is exercised the same way it would
be over a real websocket.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from thymble.connectors.home_assistant.client import ConnectionClosed

_CLOSED = object()


@dataclass
class FakeItem:
    uid: str
    summary: str
    status: str = "needs_action"  # or "completed"
    due: str | None = None
    description: str | None = None
    completed: str | None = None
    created_seq: int = 0
    touched_seq: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "status": self.status,
            "due": self.due,
            "description": self.description,
            "completed": self.completed,
        }


class FakeTransport:
    """Client side of an in-memory websocket. Text frames only."""

    def __init__(self, server: FakeHA):
        self._server = server
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.authed = False
        self.subs: dict[int, str] = {}  # sub id -> entity_id

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionClosed()
        self._server._handle(self, json.loads(text))

    async def recv(self) -> str:
        if self.closed and self._inbox.empty():
            raise ConnectionClosed()
        msg = await self._inbox.get()
        if msg is _CLOSED:
            raise ConnectionClosed()
        return msg

    async def close(self) -> None:
        self._server._drop(self)

    # server -> client
    def _push(self, payload: dict) -> None:
        if not self.closed:
            self._inbox.put_nowait(json.dumps(payload))


class FakeHA:
    def __init__(
        self,
        entities: tuple[str, ...] = ("todo.shopping_list",),
        *,
        token: str = "test-token",
        window: int = 100,
        double_fire_delay: float = 0.3,
        alexa_delay: float = 25.0,
    ):
        self.token = token
        self.window = window
        self.double_fire_delay = double_fire_delay
        self.alexa_delay = alexa_delay
        self._items: dict[str, list[FakeItem]] = {e: [] for e in entities}
        self._seq = itertools.count(1)
        self._conns: set[FakeTransport] = set()
        self._timers: set[asyncio.TimerHandle] = set()
        self.service_log: list[tuple[str, str, dict]] = []  # (service, entity, data)
        self.connect_count = 0
        self.accept_connections = True
        self.drop_after_next_service = False  # apply the write, then lose the ack

    # ------------------------------------------------------------------ state
    def visible(self, entity: str) -> list[FakeItem]:
        """The ~window most recently touched items, in list (creation) order."""
        items = self._items[entity]
        recent = sorted(items, key=lambda i: i.touched_seq)[-self.window :]
        return sorted(recent, key=lambda i: i.created_seq)

    def snapshot(self, entity: str) -> list[dict]:
        return [i.to_json() for i in self.visible(entity)]

    def all_items(self, entity: str) -> list[FakeItem]:
        """Ground truth on the Alexa side, including items outside the window."""
        return list(self._items[entity])

    def find(self, entity: str, summary: str) -> list[FakeItem]:
        return [i for i in self._items[entity] if i.summary == summary]

    def _lookup(self, entity: str, ref: str) -> FakeItem | None:
        for i in self._items[entity]:
            if i.uid == ref:
                return i
        for i in self._items[entity]:  # HA also accepts the summary
            if i.summary == ref:
                return i
        return None

    def _touch(self, item: FakeItem) -> None:
        item.touched_seq = next(self._seq)

    def _new_item(self, entity: str, summary: str, status: str = "needs_action") -> FakeItem:
        seq = next(self._seq)
        item = FakeItem(
            uid=uuid.uuid4().hex[:20],
            summary=summary,
            status=status,
            created_seq=seq,
            touched_seq=seq,
        )
        if status == "completed":
            item.completed = _now()
        self._items[entity].append(item)
        return item

    def seed(
        self,
        entity: str,
        summaries: list[str],
        status: str = "needs_action",
        completed: str | None = None,
    ) -> list[FakeItem]:
        """Pre-existing items. No events fired (they existed before anyone subscribed)."""
        items = [self._new_item(entity, s, status) for s in summaries]
        if completed is not None:
            for item in items:
                item.completed = completed
        return items

    # ----------------------------------------------------------- connections
    async def connect(self) -> FakeTransport:
        if not self.accept_connections:
            raise ConnectionClosed("connection refused")
        t = FakeTransport(self)
        self._conns.add(t)
        self.connect_count += 1
        t._push({"type": "auth_required", "ha_version": "2026.9.4"})
        return t

    def drop_connections(self) -> None:
        """Simulate the websocket dropping (HA restart, network blip)."""
        for t in list(self._conns):
            self._drop(t)

    def _drop(self, t: FakeTransport) -> None:
        if t.closed:
            return
        t.closed = True
        t._inbox.put_nowait(_CLOSED)
        self._conns.discard(t)

    # --------------------------------------------------------------- protocol
    def _handle(self, t: FakeTransport, msg: dict) -> None:
        if not t.authed:
            if msg.get("type") == "auth" and msg.get("access_token") == self.token:
                t.authed = True
                t._push({"type": "auth_ok", "ha_version": "2026.9.4"})
            else:
                t._push({"type": "auth_invalid", "message": "Invalid access token"})
                self._drop(t)
            return

        mid, mtype = msg["id"], msg["type"]
        try:
            if mtype == "todo/item/subscribe":
                entity = self._entity(msg["entity_id"])
                t.subs[mid] = entity
                t._push({"id": mid, "type": "result", "success": True, "result": None})
                t._push({"id": mid, "type": "event", "event": {"items": self.snapshot(entity)}})
            elif mtype == "ping":
                t._push({"id": mid, "type": "pong"})
            elif mtype == "todo/item/list":
                entity = self._entity(msg["entity_id"])
                t._push(
                    {
                        "id": mid,
                        "type": "result",
                        "success": True,
                        "result": {"items": self.snapshot(entity)},
                    }
                )
            elif mtype == "call_service":
                self._call_service(msg)
                if self.drop_after_next_service:
                    self.drop_after_next_service = False
                    self._drop(t)
                    return
                t._push(
                    {
                        "id": mid,
                        "type": "result",
                        "success": True,
                        "result": {"context": {"id": uuid.uuid4().hex}},
                    }
                )
            else:
                raise _Err("unknown_command", f"Unknown command {mtype}")
        except _Err as e:
            t._push(
                {
                    "id": mid,
                    "type": "result",
                    "success": False,
                    "error": {"code": e.code, "message": e.message},
                }
            )

    def _entity(self, entity: str) -> str:
        if entity not in self._items:
            raise _Err("not_found", f"Entity {entity} not found")
        return entity

    def _call_service(self, msg: dict) -> None:
        if msg.get("domain") != "todo":
            raise _Err("not_found", "only todo domain is faked")
        service = msg["service"]
        data = dict(msg.get("service_data") or {})
        entity = self._entity(
            (msg.get("target") or {}).get("entity_id") or data.pop("entity_id", None)
        )
        self.service_log.append((service, entity, data))
        if service == "add_item":
            self._new_item(entity, data["item"])
        elif service == "update_item":
            item = self._lookup(entity, data["item"])
            if item is None:
                raise _Err("service_validation_error", "Unable to find to-do list item")
            if "rename" in data:
                item.summary = data["rename"]
            if "status" in data:
                item.status = data["status"]
                item.completed = _now() if data["status"] == "completed" else None
            self._touch(item)
        elif service == "remove_item":
            refs = data["item"] if isinstance(data["item"], list) else [data["item"]]
            for ref in refs:
                item = self._lookup(entity, ref)
                if item is None:
                    raise _Err("service_validation_error", "Unable to find to-do list item")
                self._items[entity].remove(item)
        else:
            raise _Err("not_found", f"Service todo.{service} not found")
        self._changed(entity)

    def _changed(self, entity: str) -> None:
        """Fire the subscription twice with the identical snapshot payload."""
        payload = {"items": self.snapshot(entity)}
        self._fire(entity, payload)
        self._later(self.double_fire_delay, lambda: self._fire(entity, payload))

    def _fire(self, entity: str, payload: dict) -> None:
        for t in list(self._conns):
            for sid, ent in t.subs.items():
                if ent == entity:
                    t._push({"id": sid, "type": "event", "event": payload})

    def _later(self, delay: float, fn) -> None:
        loop = asyncio.get_running_loop()

        def run():
            self._timers.discard(h)
            fn()

        h = loop.call_later(delay, run)
        self._timers.add(h)

    # ------------------------------------------------- Alexa-side simulation
    def alexa_add(self, entity: str, summary: str) -> None:
        """'Alexa, add <summary> to my shopping list' — lands after alexa_delay."""

        def apply():
            self._new_item(entity, summary)
            self._changed(entity)

        self._later(self.alexa_delay, apply)

    def alexa_set_status(self, entity: str, uid: str, status: str) -> None:
        """Checked / unchecked in the Alexa app."""

        def apply():
            item = self._lookup(entity, uid)
            if item:
                item.status = status
                item.completed = _now() if status == "completed" else None
                self._touch(item)
                self._changed(entity)

        self._later(self.alexa_delay, apply)

    def alexa_rename(self, entity: str, uid: str, summary: str) -> None:
        def apply():
            item = self._lookup(entity, uid)
            if item:
                item.summary = summary
                self._touch(item)
                self._changed(entity)

        self._later(self.alexa_delay, apply)

    def alexa_remove(self, entity: str, uid: str) -> None:
        def apply():
            item = self._lookup(entity, uid)
            if item:
                self._items[entity].remove(item)
                self._changed(entity)

        self._later(self.alexa_delay, apply)

    # ---------------------------------------------------------------- testing
    @property
    def busy(self) -> bool:
        return bool(self._timers) or any(not t._inbox.empty() for t in self._conns)


class _Err(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


def _now() -> str:
    return datetime.now(UTC).isoformat()
