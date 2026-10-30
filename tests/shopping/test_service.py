"""List semantics: merging, remembered assignment, urgency/stop-gaps, learning."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from thymble.models import (
    Ingredient,
    ItemStatus,
    ItemStorePref,
    LearningEvent,
    ShoppingItemSource,
    Urgency,
)
from thymble.shopping.errors import ShoppingError

pytestmark = pytest.mark.db


def test_add_merges_same_ingredient_and_compatible_units(svc, setup):
    a = svc.add_item(setup.list.id, "milk", Decimal(1), "cup")
    b = svc.add_item(setup.list.id, "Milk", Decimal(4), "tbsp")
    assert b.merged and b.item.id == a.item.id
    assert a.item.quantity == Decimal("1.2500") and a.item.unit == "cup"
    sources = svc.session.scalars(
        select(ShoppingItemSource).where(ShoppingItemSource.shopping_item_id == a.item.id)
    ).all()
    assert len(sources) == 2
    # Incompatible units (volume vs mass, no density) stay separate items.
    c = svc.add_item(setup.list.id, "milk", Decimal(200), "g")
    assert not c.merged and c.item.id != a.item.id


def test_plural_and_case_resolve_to_one_ingredient(svc, setup):
    a = svc.add_item(setup.list.id, "Eggs").item
    b = svc.add_item(setup.list.id, "egg").item
    assert a.id == b.id
    assert len(svc.session.scalars(select(Ingredient)).all()) == 1


def test_bought_items_are_not_merge_targets(svc, setup):
    a = svc.add_item(setup.list.id, "bread").item
    svc.mark_bought(a)
    b = svc.add_item(setup.list.id, "bread").item
    assert b.id != a.id and b.status == ItemStatus.NEEDED


def test_assignment_is_remembered(svc, setup):
    first = svc.add_item(setup.list.id, "paper towels", store_id=setup.costco.id).item
    svc.mark_bought(first)
    again = svc.add_item(setup.list.id, "paper towels").item
    assert again.id != first.id and again.store_id == setup.costco.id
    other = svc.add_item(setup.list.id, "bananas", store_group_id=setup.everyday.id).item
    svc.delete_item(other)
    assert svc.add_item(setup.list.id, "banana").item.store_group_id == setup.everyday.id


def test_store_and_group_are_exclusive(svc, setup):
    with pytest.raises(ShoppingError):
        svc.add_item(setup.list.id, "x", store_id=setup.aldi.id, store_group_id=setup.everyday.id)


def test_readd_of_waiting_bulk_item_prompts_instead_of_urgent(svc, setup):
    tp = svc.add_item(setup.list.id, "toilet paper", store_id=setup.costco.id).item
    again = svc.add_item(setup.list.id, "toilet paper")
    assert again.merged and tp.urgency == Urgency.NORMAL
    assert svc.prompts == ["Need some toilet paper sooner?"]


def test_urgent_bulk_item_gets_stopgap_in_default_group(svc, setup):
    bulk = svc.add_item(setup.list.id, "toilet paper", store_id=setup.costco.id, urgent=True)
    bulk = bulk.item
    [gap] = svc.stop_gaps(bulk)
    assert gap.bridge_of == bulk.id and gap.urgency == Urgency.URGENT
    assert gap.store_group_id == setup.everyday.id
    assert svc.mirror_text(gap) == "toilet paper (small)"
    # Not urgent any more -> stop-gap withdrawn.
    svc.set_urgent(bulk, False)
    assert svc.stop_gaps(bulk) == [] and gap.deleted_at is not None


def test_stopgap_prefers_also_ok_store_and_typical_quantity(svc, setup):
    bulk = svc.add_item(setup.list.id, "coffee", store_id=setup.costco.id).item
    svc.upsert_pref(
        bulk.ingredient_id, setup.aldi.id, None, typical_quantity=Decimal(1), typical_unit="lb"
    )
    gap = svc.set_urgent(bulk, True)
    assert gap.store_id == setup.aldi.id and gap.quantity == Decimal(1) and gap.unit == "lb"


def test_non_bulk_urgent_has_no_stopgap(svc, setup):
    item = svc.add_item(setup.list.id, "milk", store_id=setup.aldi.id, urgent=True).item
    assert item.urgency == Urgency.URGENT and svc.stop_gaps(item) == []


def test_buying_stopgap_keeps_bulk_and_clears_urgency(svc, setup):
    bulk = svc.add_item(setup.list.id, "toilet paper", store_id=setup.costco.id, urgent=True)
    bulk = bulk.item
    [gap] = svc.stop_gaps(bulk)
    svc.mark_bought(gap)
    assert gap.status == ItemStatus.BOUGHT
    assert bulk.status == ItemStatus.NEEDED and bulk.urgency == Urgency.NORMAL


def test_buying_bulk_clears_both(svc, setup):
    bulk = svc.add_item(setup.list.id, "toilet paper", store_id=setup.costco.id, urgent=True)
    bulk = bulk.item
    [gap] = svc.stop_gaps(bulk)
    svc.mark_bought(bulk, store_id=setup.costco.id)
    assert gap.status == ItemStatus.BOUGHT


def test_checkoff_elsewhere_learns_also_ok_and_suggests_after_three(svc, setup):
    def buy_at(store):
        item = svc.add_item(setup.list.id, "olive oil").item
        svc.mark_bought(item, store_id=store.id)

    svc.add_item(setup.list.id, "olive oil", store_id=setup.costco.id)
    buy_at(setup.costco)
    assert svc.suggestions == []
    buy_at(setup.aldi)
    prefs = svc.session.scalars(select(ItemStorePref)).all()
    assert {(p.store_id, p.is_home) for p in prefs} == {
        (setup.costco.id, True),
        (setup.aldi.id, False),
    }
    buy_at(setup.aldi)
    assert svc.suggestions == []
    buy_at(setup.aldi)
    [s] = svc.suggestions
    assert (s.store_id, s.purchases, s.ingredient_name) == (setup.aldi.id, 3, "olive oil")
    assert [x.store_id for x in svc.pending_home_suggestions()] == [setup.aldi.id]
    events = svc.session.scalars(
        select(LearningEvent).where(LearningEvent.event_type == "item_checked")
    ).all()
    assert len(events) == 4
    # Dismissing resets the count.
    svc.dismiss_home_suggestion(s.ingredient_id, setup.aldi.id)
    assert svc.pending_home_suggestions() == []


def test_checkoff_at_group_member_counts_as_home(svc, setup):
    item = svc.add_item(setup.list.id, "bananas", store_group_id=setup.everyday.id).item
    svc.mark_bought(item, store_id=setup.walmart.id)
    event = svc.session.scalars(
        select(LearningEvent).where(LearningEvent.event_type == "item_checked")
    ).one()
    assert event.payload["at_home"] is True


def test_delete_removes_stopgaps(svc, setup):
    bulk = svc.add_item(setup.list.id, "rice", store_id=setup.costco.id, urgent=True).item
    [gap] = svc.stop_gaps(bulk)
    svc.delete_item(bulk)
    assert gap.deleted_at is not None
