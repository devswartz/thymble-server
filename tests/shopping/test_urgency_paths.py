"""Urgency and stop-gaps (D-029) behave the same through REST and the PowerSync upload.

Every scenario runs twice: once through the REST endpoints, once through
`POST /v1/sync/upload` the way a device's offline queue would send it. Both paths go
through the same ShoppingService methods, so the expectations are shared.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from tests.accounts import Member, authed_settings, create_member
from tests.shopping.test_api import make_client, ok
from thymble.models import ItemStatus, ShoppingItem, Urgency

pytestmark = pytest.mark.db

PATHS = ["rest", "sync"]


@pytest.fixture
def member(sessions, household) -> Member:
    return create_member(sessions, authed_settings(), household_id=household)


@pytest.fixture
def api(sessions, member):
    return make_client(sessions, member)


class Path:
    """One way of changing an item: REST endpoints or the sync upload."""

    def __init__(self, kind: str, api, member: Member):
        self.kind, self.api, self.member = kind, api, member

    def _upload(self, *entries: dict) -> None:
        ok(self.api.post("/v1/sync/upload", json={"batch": list(entries)}))

    def _patch(self, item_id, **data) -> None:
        self._upload({"op": "PATCH", "table": "shopping_item", "id": str(item_id), "data": data})

    def set_urgent(self, item_id, urgent: bool) -> None:
        if self.kind == "rest":
            ok(self.api.patch(f"/v1/shopping/items/{item_id}", json={"urgent": urgent}))
        else:
            self._patch(item_id, urgency="urgent" if urgent else "normal")

    def assign(self, item_id, store_id) -> None:
        if self.kind == "rest":
            body = {"assign": True, "store_id": str(store_id), "remember_store": False}
            ok(self.api.patch(f"/v1/shopping/items/{item_id}", json=body))
        else:
            self._patch(item_id, store_id=str(store_id), store_group_id=None)

    def check(self, item_id) -> None:
        if self.kind == "rest":
            ok(self.api.post(f"/v1/shopping/items/{item_id}/check"))
        else:
            now = dt.datetime.now(dt.UTC).isoformat()
            self._patch(item_id, status="bought", checked_at=now)

    def uncheck(self, item_id) -> None:
        if self.kind == "rest":
            ok(self.api.post(f"/v1/shopping/items/{item_id}/uncheck"))
        else:
            self._patch(item_id, status="needed", checked_at=None)

    def delete(self, item_id) -> None:
        if self.kind == "rest":
            ok(self.api.delete(f"/v1/shopping/items/{item_id}"), 204)
        else:
            self._upload({"op": "DELETE", "table": "shopping_item", "id": str(item_id)})


@pytest.fixture(params=PATHS)
def path(request, api, member) -> Path:
    return Path(request.param, api, member)


def add(api, setup, name: str, store) -> uuid.UUID:
    url = f"/v1/shopping/lists/{setup.list.id}/items"
    body = ok(api.post(url, json={"name": name, "store_id": str(store.id)}), 201)
    return uuid.UUID(body["item"]["id"])


def item(db, item_id) -> ShoppingItem:
    db.expire_all()
    return db.get(ShoppingItem, item_id)


def gaps(db, item_id, *, live_only: bool = True) -> list[ShoppingItem]:
    db.expire_all()
    q = select(ShoppingItem).where(ShoppingItem.bridge_of == item_id)
    if live_only:
        q = q.where(ShoppingItem.deleted_at.is_(None))
    return list(db.scalars(q))


def test_urgent_bulk_item_gets_one_stop_gap_and_clearing_removes_it(path, db, setup):
    tp = add(path.api, setup, "toilet paper", setup.costco)

    path.set_urgent(tp, True)
    assert item(db, tp).urgency == Urgency.URGENT
    [gap] = gaps(db, tp)
    assert gap.store_group_id == setup.everyday.id
    assert (gap.urgency, gap.status, gap.list_id) == (
        Urgency.URGENT,
        ItemStatus.NEEDED,
        setup.list.id,
    )

    path.set_urgent(tp, True)  # again: still one stop-gap
    assert [g.id for g in gaps(db, tp)] == [gap.id]

    path.set_urgent(tp, False)
    assert item(db, tp).urgency == Urgency.NORMAL
    assert gaps(db, tp) == []
    assert item(db, gap.id).deleted_at is not None


def test_urgent_everyday_item_gets_no_stop_gap(path, db, setup):
    milk = add(path.api, setup, "milk", setup.aldi)
    path.set_urgent(milk, True)
    assert item(db, milk).urgency == Urgency.URGENT
    assert gaps(db, milk) == []


def test_reassigning_an_urgent_item_to_a_bulk_store_adds_the_stop_gap(path, db, setup):
    tp = add(path.api, setup, "paper towels", setup.aldi)
    path.set_urgent(tp, True)
    assert gaps(db, tp) == []
    path.assign(tp, setup.costco.id)
    assert item(db, tp).store_id == setup.costco.id
    assert len(gaps(db, tp)) == 1


def test_buying_the_stop_gap_clears_only_the_urgency(path, db, setup, member):
    tp = add(path.api, setup, "toilet paper", setup.costco)
    path.set_urgent(tp, True)
    [gap] = gaps(db, tp)

    path.check(gap.id)
    bought = item(db, gap.id)
    assert bought.status == ItemStatus.BOUGHT
    assert bought.checked_by == member.user_id and bought.checked_at is not None
    bulk = item(db, tp)
    assert (bulk.status, bulk.urgency) == (ItemStatus.NEEDED, Urgency.NORMAL)


def test_buying_the_bulk_item_buys_its_stop_gap(path, db, setup):
    tp = add(path.api, setup, "toilet paper", setup.costco)
    path.set_urgent(tp, True)
    [gap] = gaps(db, tp)

    path.check(tp)
    bulk = item(db, tp)
    assert (bulk.status, bulk.urgency) == (ItemStatus.BOUGHT, Urgency.NORMAL)
    assert item(db, gap.id).status == ItemStatus.BOUGHT

    path.uncheck(tp)
    bulk = item(db, tp)
    assert (bulk.status, bulk.checked_at, bulk.checked_by) == (ItemStatus.NEEDED, None, None)


def test_deleting_the_bulk_item_deletes_its_stop_gap(path, db, setup):
    tp = add(path.api, setup, "toilet paper", setup.costco)
    path.set_urgent(tp, True)
    [gap] = gaps(db, tp)
    path.delete(tp)
    assert item(db, tp).deleted_at is not None
    assert item(db, gap.id).deleted_at is not None


def test_item_created_urgent_on_a_device_gets_its_stop_gap(api, db, setup, member):
    item_id = uuid.uuid4()
    entry = {
        "op": "PUT",
        "table": "shopping_item",
        "id": str(item_id),
        "data": {
            "household_id": str(member.household_id),
            "list_id": str(setup.list.id),
            "display_text": "dish soap",
            "status": "needed",
            "urgency": "urgent",
            "store_id": str(setup.costco.id),
        },
    }
    ok(api.post("/v1/sync/upload", json={"batch": [entry]}))
    [gap] = gaps(db, item_id)
    assert gap.store_group_id == setup.everyday.id
    # Re-sending the same PUT (a retried batch) doesn't add a second stop-gap.
    ok(api.post("/v1/sync/upload", json={"batch": [entry]}))
    assert [g.id for g in gaps(db, item_id)] == [gap.id]


def test_a_rejected_batch_leaves_no_stop_gap(api, db, setup, member):
    tp = add(api, setup, "toilet paper", setup.costco)
    batch = [
        {"op": "PATCH", "table": "shopping_item", "id": str(tp), "data": {"urgency": "urgent"}},
        {"op": "PATCH", "table": "shopping_item", "id": str(tp), "data": {"bridge_of": None}},
    ]
    response = api.post("/v1/sync/upload", json={"batch": batch})
    assert response.status_code == 422, response.text
    assert item(db, tp).urgency == Urgency.NORMAL
    assert gaps(db, tp, live_only=False) == []


def test_stop_gap_link_stays_server_owned(api, db, setup):
    tp = add(api, setup, "toilet paper", setup.costco)
    other = add(api, setup, "paper towels", setup.costco)
    entry = {
        "op": "PATCH",
        "table": "shopping_item",
        "id": str(other),
        "data": {"bridge_of": str(tp)},
    }
    response = api.post("/v1/sync/upload", json={"batch": [entry]})
    assert response.status_code == 422
    assert "bridge_of" in response.text
