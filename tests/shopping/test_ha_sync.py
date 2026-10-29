"""Home Assistant sync engine against the fake HA, backed by Postgres.

Time-compressed: HA's ~300 ms double-fire is 30 ms, Alexa's ~25 s delay is 60 ms.
Never contacts a real Home Assistant.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from tests.shopping.fake_ha import FakeHA
from thymble.connectors.home_assistant.engine import EngineOptions, HASyncEngine
from thymble.models import (
    ConnectorAccount,
    ConnectorItemLink,
    ItemStatus,
    ShoppingItem,
    TripKind,
    Urgency,
)
from thymble.shopping import trips
from thymble.shopping.service import ShoppingService

pytestmark = [pytest.mark.asyncio, pytest.mark.db]

MAIN, COSTCO = "todo.main_shopping_list", "todo.costco_list"
FAST = dict(double_fire_delay=0.03, alexa_delay=0.06)


class FakeClock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@dataclass
class Env:
    fake: FakeHA
    engine: HASyncEngine
    clock: FakeClock
    sessions: object
    household: uuid.UUID
    setup: object

    async def settle(self, timeout=5.0):
        """Wait until no fake timers, no queued events and no pending verifies."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        quiet = 0
        while quiet < 4:
            if loop.time() > end:
                raise TimeoutError("did not settle")
            await asyncio.sleep(0.01)
            busy = self.fake.busy or not self.engine.idle
            quiet = 0 if busy else quiet + 1

    def app(self, fn):
        """An app-side change (REST / PowerSync upload): own transaction, then nudge."""
        with self.sessions() as s:
            result = fn(ShoppingService(s, self.household))
            s.commit()
        self.engine.nudge()
        return result

    def items(self, entity=MAIN, status=None):
        return sorted(
            i.summary for i in self.fake.all_items(entity) if status is None or i.status == status
        )

    def uid(self, entity, summary):
        return self.fake.find(entity, summary)[0].uid

    def thymble(self) -> list[ShoppingItem]:
        with self.sessions() as s:
            return list(
                s.scalars(
                    select(ShoppingItem).where(
                        ShoppingItem.list_id == self.setup.list.id,
                        ShoppingItem.deleted_at.is_(None),
                    )
                )
            )

    def by_name(self, name, stopgap=False) -> ShoppingItem:
        hits = [
            i
            for i in self.thymble()
            if i.display_text == name and (i.bridge_of is not None) == stopgap
        ]
        assert len(hits) == 1, [(i.display_text, i.status) for i in self.thymble()]
        return hits[0]

    def links(self, item_id):
        return [lk for lk in self.engine._links_of(item_id) if not lk.suppressed]

    def prefer_costco(self, *names):
        def go(svc):
            for n in names:
                svc.set_home(svc.resolve_ingredient(n).id, self.setup.costco.id, None)

        self.app(go)

    def writes(self, service=None):
        return [w for w in self.fake.service_log if service is None or w[0] == service]


@pytest_asyncio.fixture
async def make_env(sessions, household, setup):
    made = []

    async def _make(*, fake_kw=None, seed=None, token="test-token", **opts):
        fake = FakeHA((MAIN, COSTCO), **{**FAST, **(fake_kw or {})})
        for entity, args in (seed or {}).items():
            for summaries, status, completed in args:
                fake.seed(entity, summaries, status, completed)
        with sessions() as s:
            account = ConnectorAccount(
                household_id=household,
                kind="home_assistant",
                base_url="http://fake-ha.invalid",
                config={"main_entity": MAIN},
            )
            s.add(account)
            s.flush()
            costco = s.merge(setup.costco)
            costco.external_list_connector_account_id = account.id
            costco.external_list_id = COSTCO
            s.commit()
            account_id = account.id
        clock = FakeClock()
        kw = dict(
            main_entity=MAIN,
            list_id=setup.list.id,
            dedupe_window=0.4,
            verify_delay=0.05,
            reconnect_delay=0.05,
            max_reconnect_delay=0.1,
            poll_interval=3600,
            sweep_interval=3600,
            window=fake.window,
        )
        kw.update(opts)
        engine = HASyncEngine(
            account_id, sessions, fake.connect, token, EngineOptions(**kw), clock=clock
        )
        engine.start()
        made.append(engine)
        env = Env(fake, engine, clock, sessions, household, setup)
        if token == "test-token":
            await asyncio.wait_for(engine.connected.wait(), 2)
            await env.settle()
        return env

    yield _make
    for engine in made:
        await engine.stop()


# ------------------------------------------------------------------ basics
async def test_app_add_mirrors_once_no_echo_import(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "milk"))
    await env.settle()
    assert env.items(MAIN) == ["milk"]
    assert [s for s, *_ in env.writes()] == ["add_item"]
    assert len(env.thymble()) == 1  # our own add was not re-imported
    [link] = env.links(env.by_name("milk").id)
    assert link.uid == env.uid(MAIN, "milk") and not link.awaiting_echo
    assert env.engine.stats["duplicate_events"] >= 1  # the double-fire was dropped


async def test_quantities_are_written_as_display_text(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "chicken thighs", 2, "lbs"))
    await env.settle()
    assert env.items(MAIN) == ["2 lb chicken thighs"]
    assert len(env.thymble()) == 1


async def test_alexa_add_imported_once_despite_double_events(make_env):
    env = await make_env()
    env.fake.alexa_add(MAIN, "Eggs")
    await asyncio.sleep(0.03)
    assert env.thymble() == []  # still in the Alexa->HA delay
    await env.settle()
    item = env.by_name("Eggs")
    assert item.status == ItemStatus.NEEDED
    assert env.engine.stats["imported"] == 1 and env.engine.stats["duplicate_events"] >= 1
    assert env.writes() == []  # nothing to write back


async def test_voice_quantity_is_parsed(make_env):
    env = await make_env()
    env.fake.alexa_add(MAIN, "2 lb chicken thighs")
    await env.settle()
    item = env.by_name("chicken thighs")
    assert (item.quantity, item.unit) == (2, "lb")
    assert env.writes() == []  # mirror text already matches


async def test_check_off_flows_both_ways(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "bread"))
    await env.settle()
    env.fake.alexa_set_status(MAIN, env.uid(MAIN, "bread"), "completed")
    await env.settle()
    assert env.by_name("bread").status == ItemStatus.BOUGHT
    env.fake.alexa_set_status(MAIN, env.uid(MAIN, "bread"), "needs_action")
    await env.settle()
    assert env.by_name("bread").status == ItemStatus.NEEDED
    env.app(lambda svc: svc.mark_bought(svc.get_item(env.by_name("bread").id)))
    await env.settle()
    assert env.items(MAIN, "completed") == ["bread"]
    assert env.by_name("bread").status == ItemStatus.BOUGHT  # echo didn't flip anything


async def test_delete_flows_both_ways(make_env):
    env = await make_env()
    env.app(lambda svc: [svc.add_item(env.setup.list.id, n) for n in ("bread", "jam")])
    await env.settle()
    env.app(lambda svc: svc.delete_item(svc.get_item(env.by_name("bread").id)))
    await env.settle()
    assert env.items(MAIN) == ["jam"]
    env.fake.alexa_remove(MAIN, env.uid(MAIN, "jam"))
    await env.settle()
    assert env.thymble() == []
    assert env.engine.stats["verifies"] >= 1  # removal was confirmed before acting


async def test_external_rename_is_last_writer_wins(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "milk"))
    await env.settle()
    env.fake.alexa_rename(MAIN, env.uid(MAIN, "milk"), "oat milk")
    await env.settle()
    assert [i.display_text for i in env.thymble()] == ["oat milk"]


async def test_urgent_keyword_stripped_and_renamed_on_alexa(make_env):
    env = await make_env()
    env.fake.alexa_add(MAIN, "need coffee")
    await env.settle()
    item = env.by_name("coffee")
    assert item.urgency == Urgency.URGENT
    assert env.items(MAIN) == ["coffee"]  # mirror renamed to the clean name


# --------------------------------------------------------------- duplicates
async def test_voice_duplicates_merge_and_extra_copy_is_removed(make_env):
    env = await make_env()
    env.fake.alexa_add(MAIN, "milk")
    env.fake.alexa_add(MAIN, "milk")
    await env.settle()
    item = env.by_name("milk")
    assert len(env.thymble()) == 1 and env.engine.stats["merged"] == 1
    assert env.items(MAIN) == ["milk"]  # the extra Alexa copy was removed
    [link] = env.links(item.id)
    env.fake.alexa_set_status(MAIN, link.uid, "completed")
    await env.settle()
    assert env.by_name("milk").status == ItemStatus.BOUGHT


async def test_voice_add_merges_with_existing_app_item(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "eggs"))
    await env.settle()
    env.fake.alexa_add(MAIN, "Eggs")
    await env.settle()
    assert len(env.thymble()) == 1 and len(env.links(env.by_name("eggs").id)) == 1
    assert env.items(MAIN) == ["eggs"]


async def test_voice_add_of_item_on_order_creates_a_new_needed_item(make_env):
    env = await make_env()
    walmart = env.setup.walmart.id
    env.app(lambda svc: svc.add_item(env.setup.list.id, "paper towels"))
    await env.settle()

    def order(svc):
        trip = trips.create_trip(svc, walmart, TripKind.PICKUP)
        trips.export_order(svc, trip, [env.by_name("paper towels").id])

    env.app(order)
    await env.settle()
    assert env.items(MAIN) == ["paper towels — on Walmart order"]
    env.fake.alexa_add(MAIN, "paper towels")
    await env.settle()
    statuses = sorted(i.status.value for i in env.thymble())
    assert statuses == ["needed", "on_order"]
    assert env.items(MAIN) == ["paper towels", "paper towels — on Walmart order"]


# --------------------------------------------------------------- routing
async def test_costco_item_said_to_main_list_is_moved(make_env):
    env = await make_env()
    env.prefer_costco("paper towels")
    env.fake.alexa_add(MAIN, "paper towels")
    await env.settle()
    assert env.items(MAIN) == [] and env.items(COSTCO) == ["paper towels"]
    item = env.by_name("paper towels")
    assert [lk.entity for lk in env.links(item.id)] == [COSTCO]
    assert len(env.thymble()) == 1  # one Thymble item throughout


async def test_app_add_with_costco_home_goes_straight_to_costco(make_env):
    env = await make_env()
    costco = env.setup.costco.id
    env.app(lambda svc: svc.add_item(env.setup.list.id, "olive oil", store_id=costco))
    await env.settle()
    assert env.items(COSTCO) == ["olive oil"] and env.items(MAIN) == []


async def test_changing_home_moves_the_mirror(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "rice"))
    await env.settle()
    rice = env.by_name("rice").id
    env.app(lambda svc: svc.assign(svc.get_item(rice), env.setup.costco.id, None))
    await env.settle()
    assert env.items(MAIN) == [] and env.items(COSTCO) == ["rice"]


async def test_voice_add_on_costco_list_learns_costco_home(make_env):
    env = await make_env()
    env.fake.alexa_add(COSTCO, "almonds")
    await env.settle()
    assert env.by_name("almonds").store_id == env.setup.costco.id
    assert env.items(COSTCO) == ["almonds"] and env.items(MAIN) == []
    env.fake.alexa_add(MAIN, "almonds")  # remembered: moves to Costco, merges
    await env.settle()
    assert env.items(COSTCO) == ["almonds"] and env.items(MAIN) == []
    assert len(env.thymble()) == 1


# --------------------------------------------------------------- stop-gaps
async def _urgent_tp(env):
    env.prefer_costco("toilet paper")
    await env.settle()
    env.fake.alexa_add(MAIN, "urgent toilet paper")
    await env.settle()
    return env.by_name("toilet paper"), env.by_name("toilet paper", stopgap=True)


async def test_spoken_urgent_entry_becomes_the_stopgap(make_env):
    env = await make_env()
    bulk, gap = await _urgent_tp(env)
    assert bulk.urgency == Urgency.URGENT and bulk.store_id == env.setup.costco.id
    assert gap.bridge_of == bulk.id and gap.store_group_id == env.setup.everyday.id
    assert env.items(COSTCO) == ["toilet paper"]
    assert env.items(MAIN) == ["toilet paper (small)"]
    # The spoken entry was renamed in place, not removed and re-added.
    assert [s for s, e, _ in env.writes() if e == MAIN] == ["update_item"]
    [link] = env.links(gap.id)
    assert link.uid == env.uid(MAIN, "toilet paper (small)")


async def test_buying_stopgap_leaves_bulk_and_clears_urgency(make_env):
    env = await make_env()
    bulk, gap = await _urgent_tp(env)
    env.fake.alexa_set_status(MAIN, env.uid(MAIN, "toilet paper (small)"), "completed")
    await env.settle()
    bulk, gap = env.by_name("toilet paper"), env.by_name("toilet paper", stopgap=True)
    assert gap.status == ItemStatus.BOUGHT and bulk.status == ItemStatus.NEEDED
    assert bulk.urgency == Urgency.NORMAL
    assert env.items(COSTCO, "needs_action") == ["toilet paper"]


async def test_buying_bulk_clears_both(make_env):
    env = await make_env()
    await _urgent_tp(env)
    env.fake.alexa_set_status(COSTCO, env.uid(COSTCO, "toilet paper"), "completed")
    await env.settle()
    assert env.by_name("toilet paper").status == ItemStatus.BOUGHT
    assert env.by_name("toilet paper", stopgap=True).status == ItemStatus.BOUGHT
    assert env.items(MAIN, "completed") == ["toilet paper (small)"]


async def test_urgent_toggle_in_app_creates_and_withdraws_stopgap(make_env):
    env = await make_env()
    costco = env.setup.costco.id
    env.app(lambda svc: svc.add_item(env.setup.list.id, "dish soap", store_id=costco))
    await env.settle()
    soap = env.by_name("dish soap").id
    env.app(lambda svc: svc.set_urgent(svc.get_item(soap), True))
    await env.settle()
    assert env.items(MAIN) == ["dish soap (small)"] and env.items(COSTCO) == ["dish soap"]
    env.app(lambda svc: svc.set_urgent(svc.get_item(soap), False))
    await env.settle()
    assert env.items(MAIN) == []  # stop-gap withdrawn


# --------------------------------------------------------------- pending orders
async def test_walmart_order_rename_confirm_and_missing(make_env):
    env = await make_env()
    env.app(lambda svc: [svc.add_item(env.setup.list.id, n) for n in ("milk", "eggs", "bread")])
    await env.settle()
    milk, eggs = env.by_name("milk").id, env.by_name("eggs").id

    def order(svc):
        trip = trips.create_trip(svc, env.setup.walmart.id, TripKind.PICKUP)
        trips.export_order(svc, trip, [milk, eggs])
        return trip.id

    trip_id = env.app(order)
    await env.settle()
    assert env.items(MAIN) == ["bread", "eggs — on Walmart order", "milk — on Walmart order"]
    assert len(env.thymble()) == 3  # renames were not mistaken for new items
    env.app(lambda svc: trips.confirm_order(svc, svc.get_trip(trip_id), {eggs}))
    await env.settle()
    assert env.by_name("milk").status == ItemStatus.BOUGHT
    assert env.by_name("eggs").status == ItemStatus.NEEDED
    assert env.items(MAIN, "completed") == ["milk"]  # suffix dropped once bought
    assert env.items(MAIN, "needs_action") == ["bread", "eggs"]


async def test_external_delete_of_item_on_order_only_unlinks(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "soap"))
    await env.settle()
    soap = env.by_name("soap").id

    def order(svc):
        trip = trips.create_trip(svc, env.setup.walmart.id, TripKind.PICKUP)
        trips.export_order(svc, trip, [soap])
        return trip.id

    trip_id = env.app(order)
    await env.settle()
    env.fake.alexa_remove(MAIN, env.uid(MAIN, "soap — on Walmart order"))
    await env.settle()
    assert env.by_name("soap").status == ItemStatus.ON_ORDER  # still on the order
    assert env.items(MAIN) == []  # and not put back on Alexa
    env.engine.nudge()
    await env.settle()
    assert env.items(MAIN) == []
    # Missing from the order -> needed again -> mirrored again.
    env.app(lambda svc: trips.confirm_order(svc, svc.get_trip(trip_id), {soap}))
    await env.settle()
    assert env.items(MAIN) == ["soap"]


async def test_external_delete_of_bought_item_only_unlinks(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "jam"))
    await env.settle()
    env.fake.alexa_set_status(MAIN, env.uid(MAIN, "jam"), "completed")
    await env.settle()
    env.fake.alexa_remove(MAIN, env.uid(MAIN, "jam"))
    await env.settle()
    assert env.by_name("jam").status == ItemStatus.BOUGHT and env.items(MAIN) == []


# --------------------------------------------------------------- 100-item window
async def test_missing_from_snapshot_at_window_is_not_a_delete(make_env):
    env = await make_env(fake_kw={"window": 10}, window_margin=1)
    env.app(lambda svc: svc.add_item(env.setup.list.id, "milk"))
    await env.settle()
    for i in range(10):
        env.fake.alexa_add(MAIN, f"thing {i}")
    await env.settle()
    assert "milk" not in [i["summary"] for i in env.fake.snapshot(MAIN)]  # aged out of view
    assert env.by_name("milk").status == ItemStatus.NEEDED
    assert env.engine.stats["window_misses"] >= 1
    adds_before = len(env.writes("add_item"))

    def shrink(svc):
        for i in range(5):
            svc.delete_item(svc.get_item(env.by_name(f"thing {i}").id))

    env.app(shrink)
    await env.settle()
    assert "milk" in [i["summary"] for i in env.fake.snapshot(MAIN)]
    assert len(env.writes("add_item")) == adds_before  # nothing re-added
    assert env.links(env.by_name("milk").id)[0].uid == env.uid(MAIN, "milk")


async def test_completed_items_removed_after_grace_period(make_env):
    env = await make_env(grace_period=3600)
    env.app(lambda svc: [svc.add_item(env.setup.list.id, n) for n in ("milk", "eggs")])
    await env.settle()
    env.fake.alexa_set_status(MAIN, env.uid(MAIN, "milk"), "completed")
    await env.settle()
    env.clock.advance(1800)
    env.engine.request_sweep()
    await env.settle()
    assert env.items(MAIN) == ["eggs", "milk"]  # still inside grace
    env.clock.advance(1801)
    env.engine.request_sweep()
    await env.settle()
    assert env.items(MAIN) == ["eggs"]
    assert env.by_name("milk").status == ItemStatus.BOUGHT  # Thymble keeps the history
    assert env.engine.stats["grace_removed"] == 1


# --------------------------------------------------------------- first connect
async def test_first_connect_imports_and_prunes_old_history(make_env, sessions):
    now = datetime.now(UTC)
    old = (now - timedelta(days=10)).isoformat()
    recent = (now - timedelta(days=2)).isoformat()
    env = await make_env(
        seed={
            MAIN: [
                (["bread", "urgent coffee"], "needs_action", None),
                (["Cheese"], "completed", old),
                (["Salsa"], "completed", recent),
            ],
            COSTCO: [(["almonds"], "needs_action", None)],
        }
    )
    assert env.items(MAIN) == ["Salsa", "bread", "coffee"]  # old Cheese pruned
    names = {i.display_text: i for i in env.thymble()}
    assert sorted(names) == ["Salsa", "almonds", "bread", "coffee"]
    assert names["Salsa"].status == ItemStatus.BOUGHT
    assert names["coffee"].urgency == Urgency.URGENT
    assert names["almonds"].store_id == env.setup.costco.id
    assert env.engine.stats["pruned_completed"] == 1
    with sessions() as s:
        assert s.get(ConnectorAccount, env.engine.account_id).last_synced_at is not None


# --------------------------------------------------------------- echo / stale
async def test_stale_replay_does_not_revert_state(make_env):
    """Change A then B inside the double-fire gap: A's replay arrives after B."""
    env = await make_env(fake_kw={"double_fire_delay": 0.15})
    env.app(lambda svc: svc.add_item(env.setup.list.id, "milk"))
    await env.settle()
    uid = env.uid(MAIN, "milk")
    env.fake.alexa_add(MAIN, "eggs")  # A
    env.fake.alexa_set_status(MAIN, uid, "completed")  # B (same tick, after A)
    await env.settle()
    assert env.engine.stats["stale_events"] >= 1
    assert env.by_name("milk").status == ItemStatus.BOUGHT
    assert env.items(MAIN, "completed") == ["milk"]
    assert env.by_name("eggs").status == ItemStatus.NEEDED


async def test_thymble_write_not_reverted_by_preceding_alexa_snapshot(make_env):
    env = await make_env(fake_kw={"double_fire_delay": 0.15})
    env.app(lambda svc: svc.add_item(env.setup.list.id, "milk"))
    await env.settle()
    env.fake.alexa_add(MAIN, "eggs")
    await asyncio.sleep(0.07)  # eggs landed; its replay is still pending
    env.app(lambda svc: svc.mark_bought(svc.get_item(env.by_name("milk").id)))
    await env.settle()
    assert env.by_name("milk").status == ItemStatus.BOUGHT
    assert env.items(MAIN, "completed") == ["milk"]


# --------------------------------------------------------------- reconnect
async def test_reconnect_resyncs_both_directions(make_env):
    env = await make_env()
    env.app(lambda svc: [svc.add_item(env.setup.list.id, n) for n in ("bread", "jam")])
    await env.settle()
    env.fake.accept_connections = False
    env.fake.drop_connections()
    await asyncio.sleep(0.05)
    assert not env.engine.connected.is_set()
    env.fake.alexa_add(MAIN, "butter")
    env.fake.alexa_remove(MAIN, env.uid(MAIN, "jam"))
    env.app(lambda svc: svc.add_item(env.setup.list.id, "honey"))
    await asyncio.sleep(0.15)
    assert env.items(MAIN) == ["bread", "butter"]  # honey not written yet
    env.fake.accept_connections = True
    await asyncio.wait_for(env.engine.connected.wait(), 2)
    await env.settle()
    assert env.engine.stats["connects"] == 2
    assert env.items(MAIN) == ["bread", "butter", "honey"]
    assert sorted(i.display_text for i in env.thymble()) == ["bread", "butter", "honey"]


async def test_reconnect_with_nothing_changed_is_a_noop(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "bread"))
    await env.settle()
    writes = len(env.writes())
    env.fake.drop_connections()
    await asyncio.sleep(0.1)
    await asyncio.wait_for(env.engine.connected.wait(), 2)
    await env.settle()
    assert len(env.writes()) == writes and len(env.thymble()) == 1


async def test_restart_reloads_links_from_the_database(make_env, sessions):
    env = await make_env()
    env.app(lambda svc: [svc.add_item(env.setup.list.id, n) for n in ("bread", "jam")])
    await env.settle()
    with sessions() as s:
        rows = s.scalars(select(ConnectorItemLink)).all()
        assert sorted(r.last_synced_state["summary"] for r in rows) == ["bread", "jam"]
    await env.engine.stop()
    writes = len(env.writes())
    fresh = HASyncEngine(
        env.engine.account_id, sessions, env.fake.connect, "test-token", env.engine.opts,
        clock=env.clock,
    )  # fmt: skip
    fresh.start()
    env.engine = fresh
    try:
        await asyncio.wait_for(fresh.connected.wait(), 2)
        await env.settle()
        assert len(env.writes()) == writes  # nothing re-added, nothing re-imported
        assert len(env.thymble()) == 2
    finally:
        await fresh.stop()


# --------------------------------------------------------------- safety
async def test_dry_run_only_logs_intended_writes(make_env):
    env = await make_env(dry_run=True)
    env.prefer_costco("paper towels")
    await env.settle()
    env.fake.alexa_add(MAIN, "paper towels")
    await env.settle()
    assert env.writes() == []
    assert [(s, e) for s, e, _ in env.engine.write_log] == [
        ("remove_item", MAIN),
        ("add_item", COSTCO),
    ]
    assert env.by_name("paper towels").store_id == env.setup.costco.id


async def test_bad_token_never_connects_and_reports_error(make_env, sessions):
    env = await make_env(token="wrong")
    await asyncio.sleep(0.1)
    assert not env.engine.connected.is_set() and env.writes() == []
    with sessions() as s:
        error = s.get(ConnectorAccount, env.engine.account_id).last_error
    assert "rejected the access token" in error


async def test_add_applied_but_ack_lost_does_not_duplicate(make_env):
    env = await make_env()
    env.fake.drop_after_next_service = True
    env.app(lambda svc: svc.add_item(env.setup.list.id, "cereal"))
    await asyncio.sleep(0.1)
    await asyncio.wait_for(env.engine.connected.wait(), 2)
    await env.settle()
    assert env.engine.stats["connects"] == 2
    assert env.items(MAIN) == ["cereal"]  # exactly one copy on Alexa
    assert env.links(env.by_name("cereal").id)[0].uid == env.uid(MAIN, "cereal")


async def test_update_ack_lost_is_still_applied(make_env):
    env = await make_env()
    env.app(lambda svc: svc.add_item(env.setup.list.id, "cereal"))
    await env.settle()
    env.fake.drop_after_next_service = True
    env.app(lambda svc: svc.mark_bought(svc.get_item(env.by_name("cereal").id)))
    await asyncio.sleep(0.1)
    await asyncio.wait_for(env.engine.connected.wait(), 2)
    await env.settle()
    assert env.items(MAIN, "completed") == ["cereal"]
    assert env.by_name("cereal").status == ItemStatus.BOUGHT
