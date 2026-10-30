"""Home Assistant `todo` <-> Thymble shopping list sync engine (architecture §6.2-§6.4).

Thymble's list (Postgres) is the source of truth; each HA `todo` entity is a mirror
(D-002). One engine runs per connector account, as a long-lived task in the worker.

    HA subscription snapshot ─► queue ─► worker task (one at a time):
        dedupe / stale check ─► diff vs. links ─► ShoppingService changes (one DB tx)
        ─► flush: reconcile each changed item's desired mirror ─► HA service calls
    App changes (REST, PowerSync uploads) ─► poll by updated_at ─► flush

Rules (ported from spikes/ha-connector, design questions resolved):
* Double events: identical consecutive snapshots are dropped; a snapshot equal to an
  older recent one is a stale replay and is re-checked with `todo/item/list`.
* Echo guard: each link stores the summary/status Thymble expects HA to show; a snapshot
  matching it is our own write coming back. New uids are matched to in-flight adds by
  summary. Removed uids are tombstoned so stale snapshots can't resurrect them.
* Window safety: HA shows only ~100 items. A uid missing from a snapshot is a delete
  only if a re-fetch confirms it while the list is clearly under the window.
* First connect imports existing items; completed ones older than 7 days are removed
  from HA instead. Completed mirrors are removed after a grace period (default 24 h).
* Voice "urgent X" for a bulk-store item turns that spoken entry into the stop-gap
  ("X (small)"). Merged voice duplicates keep one Thymble item and remove the extra
  external copy. External deletes delete the item unless it is bought or on order
  (then the link is dropped and the item is not re-added). Conflicts: last writer wins.
* Reconnect: the first snapshot per entity on each connection is authoritative; writes
  that failed while offline stay dirty and flush after resubscribe.
* Dry run: reads and subscriptions are live; every intended write is only logged.

Database work is short synchronous SQLAlchemy transactions on the event loop. Lists are
small (HA caps them at ~100 visible items), so each transaction takes milliseconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from thymble.connectors.home_assistant.client import (
    AuthError,
    ConnectionClosed,
    HAClient,
    HAError,
    MaybeSent,
    Transport,
)
from thymble.models import (
    ConnectorAccount,
    ConnectorItemLink,
    ItemSourceType,
    ItemStatus,
    ShoppingItem,
    Store,
    StoreGroupMember,
    Urgency,
)
from thymble.shopping.service import ShoppingService, utcnow
from thymble.shopping.text import name_key, parse_entry
from thymble.units import normalize as normalize_unit

log = logging.getLogger("thymble.connectors.home_assistant")

NEEDS_ACTION, COMPLETED = "needs_action", "completed"
KIND = "home_assistant"


@dataclass
class Link:
    """In-memory view of a `connector_item_link` row (the engine is its only writer)."""

    id: uuid.UUID
    item_id: uuid.UUID
    entity: str
    uid: str | None  # None while our add is in flight (or for a suppression marker)
    expected_summary: str
    expected_status: str
    completed_at: float | None = None  # when we saw/set completed (grace timer)
    awaiting_echo: bool = False  # our write sent, echo not seen yet
    unconfirmed: bool = False  # socket died before HA acked our write
    suppressed: bool = False  # removed externally while on order: don't re-add
    seq: int = 0  # creation order; the oldest link on an entity is kept

    @property
    def external_id(self) -> str:
        if self.uid is not None:
            return self.uid
        return f"{'removed' if self.suppressed else 'pending'}:{self.id}"

    def state(self) -> dict[str, Any]:
        return {
            "summary": self.expected_summary,
            "status": self.expected_status,
            "completed_at": self.completed_at,
            "awaiting_echo": self.awaiting_echo,
            "unconfirmed": self.unconfirmed,
            "suppressed": self.suppressed,
        }


@dataclass
class Desired:
    entity: str | None
    summary: str
    status: str
    item_status: ItemStatus | None


@dataclass
class EngineOptions:
    main_entity: str
    list_id: uuid.UUID | None = None
    dry_run: bool = False
    grace_period: float = 24 * 3600
    initial_completed_max_age: float = 7 * 24 * 3600
    window: int = 100
    window_margin: int = 5
    dedupe_window: float = 2.0
    verify_delay: float = 2.0
    reconnect_delay: float = 5.0
    max_reconnect_delay: float = 300.0
    poll_interval: float = 2.0
    poll_overlap: float = 10.0
    sweep_interval: float = 900.0

    @classmethod
    def from_config(cls, config: dict[str, Any] | None, **overrides: Any) -> EngineOptions:
        config = dict(config or {})
        kw: dict[str, Any] = {}
        if not config.get("main_entity") and "main_entity" not in overrides:
            raise ValueError("connector config needs main_entity (the todo.* entity)")
        if config.get("main_entity"):
            kw["main_entity"] = config["main_entity"]
        if config.get("list_id"):
            kw["list_id"] = uuid.UUID(str(config["list_id"]))
        if "dry_run" in config:
            kw["dry_run"] = bool(config["dry_run"])
        if "grace_period_hours" in config:
            kw["grace_period"] = float(config["grace_period_hours"]) * 3600
        kw.update(overrides)
        return cls(**kw)


@dataclass
class _Tx:
    session: Session
    svc: ShoppingService
    touched: set[uuid.UUID] = field(default_factory=set)


class HASyncEngine:
    def __init__(
        self,
        account_id: uuid.UUID,
        session_factory: sessionmaker,
        connect: Callable[[], Awaitable[Transport]],
        token: str,
        options: EngineOptions,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.account_id = account_id
        self._sessions = session_factory
        self._connect = connect
        self._token = token
        self.opts = options
        self.clock = clock
        self.main_entity = options.main_entity
        self.dry_run = options.dry_run

        self.household_id: uuid.UUID | None = None
        self.list_id: uuid.UUID | None = options.list_id
        self.store_entities: dict[uuid.UUID, str] = {}  # store id -> todo entity
        self.group_entities: dict[uuid.UUID, str] = {}  # store group id -> todo entity
        self.entity_stores: dict[str, uuid.UUID] = {}  # non-main entity -> store id
        self.entities: list[str] = [self.main_entity]

        self.links: dict[uuid.UUID, Link] = {}
        self._index: dict[tuple[str, str], uuid.UUID] = {}  # (entity, uid) -> link id
        self._seq = 0
        self._changed_links: set[uuid.UUID] = set()
        self._removed_links: set[uuid.UUID] = set()
        self.dirty: set[uuid.UUID] = set()
        self._pending_removals: list[tuple[str, str]] = []

        self._seen: dict[str, dict[str, tuple[str, str]]] = {}
        self._recent: dict[str, deque] = {}
        self._last_hash: dict[str, str | None] = {}
        self._tombstones: dict[str, set[str]] = {}
        self._initial_import: set[str] = set()
        self._awaiting_first: set[str] = set()
        self._ready = False
        self._watermark: datetime | None = None

        self._queue: asyncio.Queue = asyncio.Queue()
        self._busy = False
        self._client: HAClient | None = None
        self._verify_pending: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False
        self.connected = asyncio.Event()
        self.write_log: list[tuple[str, str, dict]] = []  # every write (or intended write)
        self.stats: Counter = Counter()

    # ============================================================== lifecycle
    def load(self) -> None:
        """Read account, routing and links from the database."""
        with self._sessions() as s:
            account = s.get(ConnectorAccount, self.account_id)
            if account is None or account.deleted_at is not None:
                raise LookupError(f"connector account {self.account_id} not found")
            self.household_id = account.household_id
            svc = ShoppingService(s, account.household_id)
            if self.list_id is None:
                self.list_id = svc.default_list().id
            else:
                svc.get_list(self.list_id)
            self._load_routing(s)
            first_connect = account.last_synced_at is None
            rows = s.scalars(
                select(ConnectorItemLink)
                .where(
                    ConnectorItemLink.connector_account_id == self.account_id,
                    ConnectorItemLink.deleted_at.is_(None),
                )
                .order_by(ConnectorItemLink.created_at, ConnectorItemLink.id)
            ).all()
            for row in rows:
                st = row.last_synced_state or {}
                uid = (
                    None
                    if row.external_id.startswith(("pending:", "removed:"))
                    else (row.external_id)
                )
                link = Link(
                    id=row.id,
                    item_id=row.shopping_item_id,
                    entity=row.external_list_id,
                    uid=uid,
                    expected_summary=st.get("summary", ""),
                    expected_status=st.get("status", NEEDS_ACTION),
                    completed_at=st.get("completed_at"),
                    awaiting_echo=bool(st.get("awaiting_echo")),
                    unconfirmed=bool(st.get("unconfirmed")),
                    suppressed=bool(st.get("suppressed")),
                    seq=self._next_seq(),
                )
                self._register(link)
            s.commit()
        for e in self.entities:
            self._seen.setdefault(e, {})
            self._recent.setdefault(e, deque())
            self._last_hash.setdefault(e, None)
            self._tombstones.setdefault(e, set())
        if first_connect:
            self._initial_import = set(self.entities)

    def _load_routing(self, s: Session) -> None:
        stores = s.scalars(
            select(Store).where(
                Store.household_id == self.household_id,
                Store.deleted_at.is_(None),
                Store.external_list_connector_account_id == self.account_id,
                Store.external_list_id.is_not(None),
            )
        ).all()
        self.store_entities = {st.id: st.external_list_id for st in stores}
        self.entity_stores = {}
        for st in stores:
            if st.external_list_id != self.main_entity:
                self.entity_stores.setdefault(st.external_list_id, st.id)
        # A group routes to a store list when every member store maps to that list.
        members: dict[uuid.UUID, set[str]] = {}
        for gid, sid in s.execute(
            select(StoreGroupMember.store_group_id, StoreGroupMember.store_id).where(
                StoreGroupMember.household_id == self.household_id,
                StoreGroupMember.deleted_at.is_(None),
            )
        ):
            members.setdefault(gid, set()).add(self.store_entities.get(sid, self.main_entity))
        self.group_entities = {g: next(iter(es)) for g, es in members.items() if len(es) == 1}
        entities = [self.main_entity]
        for e in self.store_entities.values():
            if e not in entities:
                entities.append(e)
        if not self.connected.is_set():
            self.entities = entities

    def start(self) -> None:
        if self.household_id is None:
            self.load()
        self._spawn(self._worker())
        self._spawn(self._run())
        self._spawn(self._ticker(self.opts.poll_interval, ("poll",)))
        self._spawn(self._ticker(self.opts.sweep_interval, ("sweep",)))

    async def stop(self) -> None:
        self._stopping = True
        if self._client:
            await self._client.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _ticker(self, interval: float, msg: tuple) -> None:
        while True:
            await asyncio.sleep(interval)
            self._queue.put_nowait(msg)

    def nudge(self) -> None:
        """Look for app-side changes now instead of at the next poll."""
        self._queue.put_nowait(("poll",))

    def request_sweep(self) -> None:
        self._queue.put_nowait(("sweep",))

    @property
    def idle(self) -> bool:
        return self._queue.empty() and not self._busy and not self._verify_pending

    async def _run(self) -> None:
        delay = self.opts.reconnect_delay
        while not self._stopping:
            client = None
            try:
                transport = await self._connect()
                client = HAClient(transport, self._token)
                await client.start()
                self._client = client
                self._ready = False
                self._awaiting_first = set(self.entities)
                for entity in self.entities:
                    first = [True]

                    def on_event(ev, entity=entity, first=first):
                        self._queue.put_nowait(("snapshot", entity, ev.get("items", []), first[0]))
                        first[0] = False

                    await client.subscribe_todo(entity, on_event)
                self.stats["connects"] += 1
                self.connected.set()
                self._record_status(None)
                delay = self.opts.reconnect_delay
                await client.closed.wait()
                self.stats["disconnects"] += 1
                log.warning("HA websocket closed (account %s); reconnecting", self.account_id)
            except AuthError as exc:
                log.warning("HA rejected the token (account %s): %s", self.account_id, exc)
                self._record_status(f"Home Assistant rejected the access token: {exc}")
                delay = self.opts.max_reconnect_delay
            except (ConnectionClosed, HAError, OSError) as exc:
                log.warning("HA connection failed (account %s): %r", self.account_id, exc)
                self._record_status(f"Can't reach Home Assistant: {exc!r}")
            finally:
                self.connected.clear()
                self._ready = False
                if self._client is client:
                    self._client = None
                if client is not None:
                    await client.close()
            if not self._stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.opts.max_reconnect_delay)

    def _record_status(self, error: str | None) -> None:
        try:
            with self._sessions() as s:
                account = s.get(ConnectorAccount, self.account_id)
                if account is not None:
                    account.last_error = error
                    s.commit()
        except Exception:
            log.exception("could not record connector status")

    # ================================================================= worker
    async def _worker(self) -> None:
        while True:
            msg = await self._queue.get()
            self._busy = True
            try:
                kind = msg[0]
                if kind == "snapshot":
                    _, entity, items, authoritative = msg
                    self._on_snapshot(entity, items, authoritative)
                elif kind == "verify":
                    if self._client is not None:
                        items = await self._client.list_items(msg[1])
                        self.stats["verifies"] += 1
                        self._on_snapshot(msg[1], items, authoritative=True)
                elif kind == "poll":
                    self._poll()
                elif kind == "sweep":
                    await self._sweep()
                await self._flush()
            except ConnectionClosed:
                pass  # dirty items stay dirty; flushed after reconnect
            except Exception:
                log.exception("HA sync error on %s (account %s)", msg[0], self.account_id)
            finally:
                self._busy = False
                self._queue.task_done()

    def _schedule_verify(self, entity: str) -> None:
        if entity in self._verify_pending:
            return
        self._verify_pending.add(entity)

        async def later():
            try:
                await asyncio.sleep(self.opts.verify_delay)
                self._queue.put_nowait(("verify", entity))
            finally:
                self._verify_pending.discard(entity)

        self._spawn(later())

    # ============================================================= db helpers
    @contextmanager
    def _tx(self) -> Iterator[_Tx]:
        with self._sessions() as s:
            tx = _Tx(s, ShoppingService(s, self.household_id))
            yield tx
            self._persist_links(s)
            s.commit()
        self.dirty |= tx.svc.touched | tx.touched

    def _persist_links(self, s: Session) -> None:
        if self._removed_links:
            s.execute(
                delete(ConnectorItemLink).where(ConnectorItemLink.id.in_(self._removed_links))
            )
            self._removed_links.clear()
        now = utcnow()
        for link_id in list(self._changed_links):
            link = self.links.get(link_id)
            if link is None:
                continue
            row = s.get(ConnectorItemLink, link_id)
            if row is None:
                row = ConnectorItemLink(
                    id=link.id,
                    household_id=self.household_id,
                    connector_account_id=self.account_id,
                )
                s.add(row)
            row.shopping_item_id = link.item_id
            row.external_list_id = link.entity
            row.external_id = link.external_id
            row.last_synced_state = link.state()
            row.last_synced_at = now
        self._changed_links.clear()

    def _save_links(self) -> None:
        if not (self._changed_links or self._removed_links):
            return
        with self._sessions() as s:
            self._persist_links(s)
            s.commit()

    # ================================================================== links
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _register(self, link: Link) -> None:
        self.links[link.id] = link
        if link.uid is not None:
            self._index[(link.entity, link.uid)] = link.id

    def _new_link(self, item_id: uuid.UUID, entity: str, uid: str | None, summary: str,
                  status: str, **kw: Any) -> Link:  # fmt: skip
        link = Link(
            id=uuid.uuid4(),
            item_id=item_id,
            entity=entity,
            uid=uid,
            expected_summary=summary,
            expected_status=status,
            seq=self._next_seq(),
            **kw,
        )
        self._register(link)
        self._changed_links.add(link.id)
        return link

    def _changed(self, link: Link) -> None:
        self._changed_links.add(link.id)

    def _drop_link(self, link: Link) -> None:
        self.links.pop(link.id, None)
        if link.uid is not None:
            self._index.pop((link.entity, link.uid), None)
        self._changed_links.discard(link.id)
        self._removed_links.add(link.id)

    def _restore_link(self, link: Link) -> None:
        self._register(link)
        self._removed_links.discard(link.id)

    def _links_of(self, item_id: uuid.UUID) -> list[Link]:
        return sorted((lk for lk in self.links.values() if lk.item_id == item_id),
                      key=lambda lk: lk.seq)  # fmt: skip

    def _lookup(self, entity: str, uid: str) -> Link | None:
        link_id = self._index.get((entity, uid))
        return self.links.get(link_id) if link_id else None

    # ================================================================ routing
    def _route(self, item: ShoppingItem) -> str:
        if item.store_id is not None:
            return self.store_entities.get(item.store_id, self.main_entity)
        if item.store_group_id is not None:
            return self.group_entities.get(item.store_group_id, self.main_entity)
        return self.main_entity

    def _desired(self, item_id: uuid.UUID) -> Desired:
        with self._sessions() as s:
            item = s.get(ShoppingItem, item_id)
            if (
                item is None
                or item.deleted_at is not None
                or item.household_id != self.household_id
                or item.list_id != self.list_id
            ):
                return Desired(None, "", "", None)
            svc = ShoppingService(s, self.household_id)
            status = COMPLETED if item.status == ItemStatus.BOUGHT else NEEDS_ACTION
            return Desired(self._route(item), svc.mirror_text(item), status, item.status)

    # ================================================================ inbound
    def _on_snapshot(self, entity: str, items: list[dict], authoritative: bool) -> None:
        if entity not in self._seen:
            return
        h = hashlib.sha1(json.dumps(items, sort_keys=True).encode()).hexdigest()
        now = asyncio.get_running_loop().time()
        recent = self._recent[entity]
        while recent and now - recent[0][1] > self.opts.dedupe_window:
            recent.popleft()
        if not authoritative:
            if h == self._last_hash[entity]:
                self.stats["duplicate_events"] += 1
                return
            if any(rh == h for rh, _ in recent):
                # An older snapshot replayed after a newer one: don't trust it.
                self.stats["stale_events"] += 1
                self._schedule_verify(entity)
                return
        recent.append((h, now))
        self._last_hash[entity] = h
        self.stats["snapshots_processed"] += 1

        with self._tx() as tx:
            self._diff(tx, entity, items, authoritative)
            if authoritative and entity in self._awaiting_first:
                self._awaiting_first.discard(entity)
                self._initial_import.discard(entity)
                if not self._awaiting_first and not self._ready:
                    self._ready = True
                    self._mark_all_dirty(tx)
                    account = tx.session.get(ConnectorAccount, self.account_id)
                    if account is not None:
                        account.last_synced_at = utcnow()

    def _mark_all_dirty(self, tx: _Tx) -> None:
        tx.touched |= set(
            tx.session.scalars(
                select(ShoppingItem.id).where(
                    ShoppingItem.household_id == self.household_id,
                    ShoppingItem.list_id == self.list_id,
                    ShoppingItem.deleted_at.is_(None),
                )
            )
        )
        tx.touched |= {lk.item_id for lk in self.links.values()}

    def _diff(self, tx: _Tx, entity: str, items: list[dict], authoritative: bool) -> None:
        seen = self._seen[entity]
        tomb = self._tombstones[entity]
        current = {it["uid"]: it for it in items if it.get("uid")}
        under_window = len(items) < self.opts.window - self.opts.window_margin

        for uid, it in current.items():
            seen[uid] = (it.get("summary") or "", it.get("status") or NEEDS_ACTION)
            if uid in tomb:
                continue  # we removed it; this view predates the removal
            link = self._lookup(entity, uid)
            if link is None:
                self._on_new_uid(tx, entity, it)
                continue
            state = seen[uid]
            expected = (link.expected_summary, link.expected_status)
            if link.awaiting_echo:
                if state == expected:
                    link.awaiting_echo = link.unconfirmed = False
                    self._changed(link)
                    self.stats["echoes"] += 1
                    continue
                if not authoritative:
                    self._schedule_verify(entity)  # probably a pre-write view
                    continue
                link.awaiting_echo = False
                self._changed(link)
                if link.unconfirmed:
                    # Our write was lost with the socket: push it again.
                    link.unconfirmed = False
                    link.expected_summary, link.expected_status = state
                    tx.touched.add(link.item_id)
                    continue
            if state != expected:
                self._on_external_change(tx, link, it)

        # In-flight adds missing from an authoritative view were lost: retry them.
        if authoritative and not self.dry_run:
            for link in list(self.links.values()):
                if link.entity == entity and link.uid is None and not link.suppressed:
                    self._drop_link(link)
                    tx.touched.add(link.item_id)
                    self.stats["lost_adds_retried"] += 1

        for uid in list(seen):
            if uid in current:
                continue
            if uid in tomb:
                del seen[uid]
                continue
            link = self._lookup(entity, uid)
            if link is None:
                del seen[uid]
                continue
            if not under_window:
                self.stats["window_misses"] += 1  # likely just aged out of view
                continue
            if not authoritative:
                self._schedule_verify(entity)
                continue
            del seen[uid]
            self._on_external_remove(tx, link)

    def _on_new_uid(self, tx: _Tx, entity: str, it: dict) -> None:
        uid = it["uid"]
        summary = it.get("summary") or ""
        status = it.get("status") or NEEDS_ACTION
        # 1) Our own add coming back?
        for link in sorted(self.links.values(), key=lambda lk: lk.seq):
            if (
                link.entity == entity
                and link.uid is None
                and not link.suppressed
                and link.expected_summary == summary
            ):
                link.uid = uid
                self._index[(entity, uid)] = link.id
                if status == link.expected_status:
                    link.awaiting_echo = link.unconfirmed = False
                self._changed(link)
                tx.touched.add(link.item_id)  # may need follow-up (moved/renamed meanwhile)
                self.stats["echoes"] += 1
                return
        # 2) Something added on the HA / Alexa side.
        completed_at = None
        if status == COMPLETED:
            if entity not in self._initial_import:
                self.stats["ignored_completed"] += 1
                return
            completed_at = _parse_ts(it.get("completed")) or self.clock()
            if self.clock() - completed_at > self.opts.initial_completed_max_age:
                # First connect: old history is pruned from HA, not imported.
                self._tombstones[entity].add(uid)
                self._pending_removals.append((entity, uid))
                self.stats["pruned_completed"] += 1
                return
        svc = tx.svc
        parsed = parse_entry(summary, svc.store_names())
        if not parsed.name.strip():
            return
        ingredient = svc.resolve_ingredient(parsed.name)
        has_home = ingredient is not None and svc.home_of(ingredient.id) is not None
        store_id = self.entity_stores.get(entity)
        hinted = svc.store_for_hint(parsed.store_hint)
        if hinted is not None:
            store_id = hinted.id  # a store said out loud ("bread from aldi") wins
        elif store_id is not None and has_home:
            store_id = None  # a remembered home wins over the list it was said to
        result = svc.add_item(
            self.list_id,
            parsed.name,
            parsed.quantity,
            parsed.unit,
            store_id=store_id,
            remember=not (hinted is not None and has_home),
            ingredient_id=ingredient.id if ingredient else None,
            resolve=False,
            source_type=ItemSourceType.CONNECTOR,
            connector_account_id=self.account_id,
            merge=status != COMPLETED,
        )
        item = result.item
        self.stats["merged" if result.merged else "imported"] += 1
        link = self._new_link(item.id, entity, uid, summary, status, completed_at=completed_at)
        if status == COMPLETED:
            svc.mark_bought(item)
        elif parsed.urgent:
            gap = svc.set_urgent(item, True)
            if gap is not None and self._route(gap) == entity and self._route(item) != entity:
                link.item_id = gap.id  # the spoken entry becomes the stop-gap

    def _on_external_change(self, tx: _Tx, link: Link, it: dict) -> None:
        summary = it.get("summary") or ""
        status = it.get("status") or NEEDS_ACTION
        self.stats["external_changes"] += 1
        svc = tx.svc
        item = tx.session.get(ShoppingItem, link.item_id)
        if item is not None and item.deleted_at is None:
            if summary != link.expected_summary and item.bridge_of is None:
                p = parse_entry(summary)
                renamed = name_key(p.name) != name_key(item.display_text)
                requantified = p.quantity != item.quantity or (
                    p.quantity is not None and normalize_unit(p.unit) != item.unit
                )
                if p.name.strip() and (renamed or requantified):
                    svc.update_item(
                        item,
                        display_text=p.name,
                        quantity=p.quantity,
                        unit=p.unit,
                        clear_quantity=p.quantity is None,
                    )
                if p.urgent and item.urgency != Urgency.URGENT:
                    svc.set_urgent(item, True)
            if status != link.expected_status:
                if status == COMPLETED:
                    link.completed_at = self.clock()
                    svc.mark_bought(item)
                else:
                    link.completed_at = None
                    svc.mark_needed(item)
        link.expected_summary, link.expected_status = summary, status
        self._changed(link)
        tx.touched.add(link.item_id)

    def _on_external_remove(self, tx: _Tx, link: Link) -> None:
        self.stats["external_removes"] += 1
        self._drop_link(link)
        item = tx.session.get(ShoppingItem, link.item_id)
        if item is None or item.deleted_at is not None:
            return
        live = [
            lk
            for lk in self._links_of(item.id)
            if (lk.uid is not None or lk.awaiting_echo) and not lk.suppressed
        ]
        if not live:
            if item.status == ItemStatus.NEEDED:
                tx.svc.delete_item(item)  # delete flows HA -> Thymble
            elif item.status == ItemStatus.ON_ORDER:
                # Just unlink; remember not to put it back while it is on order.
                self._new_link(item.id, link.entity, None, "", NEEDS_ACTION, suppressed=True)
        tx.touched.add(item.id)

    # ================================================================== poll
    def _poll(self) -> None:
        with self._sessions() as s:
            db_now = s.scalar(select(func.now()))
            q = select(ShoppingItem.id).where(
                ShoppingItem.household_id == self.household_id,
                ShoppingItem.list_id == self.list_id,
            )
            if self._watermark is not None:
                since = self._watermark - timedelta(seconds=self.opts.poll_overlap)
                q = q.where(ShoppingItem.updated_at >= since)
            self.dirty |= set(s.scalars(q))
            self._watermark = db_now
            self._load_routing(s)
            s.rollback()

    # =============================================================== outbound
    async def _flush(self) -> None:
        if not self._ready:
            return
        try:
            while self._pending_removals:
                entity, uid = self._pending_removals[0]
                with contextlib.suppress(HAError):  # already gone
                    await self._write("remove_item", entity, {"item": uid})
                self._pending_removals.pop(0)
            while self.dirty:
                item_id = self.dirty.pop()
                try:
                    await self._reconcile(item_id)
                except ConnectionClosed:
                    self.dirty.add(item_id)
                    raise
        finally:
            self._save_links()

    async def _reconcile(self, item_id: uuid.UUID) -> None:
        d = self._desired(item_id)
        for link in self._links_of(item_id):
            if link.suppressed and (
                link.entity != d.entity or d.item_status != ItemStatus.ON_ORDER
            ):
                self._drop_link(link)  # no longer on order: mirror normally again
        keeper: Link | None = None
        for link in self._links_of(item_id):
            if link.suppressed:
                continue
            if link.entity == d.entity and keeper is None:
                keeper = link
                continue
            if link.uid is None:
                continue  # add in flight; reconciled again when its uid shows up
            # Routing move, deleted item, or an extra copy of a merged duplicate.
            await self._remove_link(link)

        if keeper is not None and keeper.uid is not None:
            await self._update(keeper, d)
        suppressed = any(lk.suppressed and lk.entity == d.entity for lk in self._links_of(item_id))
        if (
            d.entity is not None
            and keeper is None
            and not suppressed
            and d.item_status != ItemStatus.BOUGHT
        ):
            link = self._new_link(item_id, d.entity, None, d.summary, NEEDS_ACTION,
                                  awaiting_echo=True)  # fmt: skip
            # The link exists before the write is sent: that is the echo guard.
            try:
                await self._write("add_item", d.entity, {"item": d.summary})
            except MaybeSent:
                link.unconfirmed = True
                self._changed(link)
                raise
            except ConnectionClosed:
                self._drop_link(link)
                self._removed_links.discard(link.id)
                raise

    async def _update(self, link: Link, d: Desired) -> None:
        changes: dict[str, str] = {}
        if link.expected_summary != d.summary:
            changes["rename"] = d.summary
        if link.expected_status != d.status:
            changes["status"] = d.status
        if not changes:
            return
        prev = (link.expected_summary, link.expected_status, link.completed_at)
        link.expected_summary, link.expected_status = d.summary, d.status
        link.awaiting_echo = True
        if d.status == COMPLETED and prev[1] != COMPLETED:
            link.completed_at = self.clock()
        elif d.status == NEEDS_ACTION:
            link.completed_at = None
        self._changed(link)
        try:
            await self._write("update_item", link.entity, {"item": link.uid, **changes})
        except MaybeSent:
            link.unconfirmed = True
            raise
        except ConnectionClosed:
            link.expected_summary, link.expected_status, link.completed_at = prev
            link.awaiting_echo = False
            raise
        except HAError:
            log.warning("update of %s failed (gone from HA?)", link.uid)
            self._drop_link(link)

    async def _remove_link(self, link: Link) -> None:
        self._drop_link(link)
        self._tombstones[link.entity].add(link.uid)
        try:
            await self._write("remove_item", link.entity, {"item": link.uid})
        except MaybeSent:
            raise  # stays tombstoned; the resync shows whether it is gone
        except ConnectionClosed:
            self._restore_link(link)
            self._tombstones[link.entity].discard(link.uid)
            raise
        except HAError:
            pass  # already gone

    async def _sweep(self) -> None:
        """Remove completed mirrors older than the grace period (keeps HA < window)."""
        if not self._ready:
            return
        now = self.clock()
        for link in list(self.links.values()):
            if (
                link.uid is not None
                and not link.suppressed
                and link.expected_status == COMPLETED
                and link.completed_at is not None
                and now - link.completed_at >= self.opts.grace_period
            ):
                await self._remove_link(link)
                self.stats["grace_removed"] += 1

    async def _write(self, service: str, entity: str, data: dict) -> None:
        self.write_log.append((service, entity, data))
        if self.dry_run:
            log.info("DRY RUN (account %s): todo.%s on %s %s", self.account_id, service,
                     entity, data)  # fmt: skip
            return
        if self._client is None:
            raise ConnectionClosed()
        self.stats[f"write_{service}"] += 1
        await self._client.call_service(service, entity, data)


def _parse_ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
