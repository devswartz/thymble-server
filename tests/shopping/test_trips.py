"""Trips: in-store sections, pickup export, confirmation and reminders (§6.4, D-030)."""

from datetime import timedelta

import pytest

from thymble.models import ItemStatus, TripKind, TripStatus
from thymble.shopping import trips
from thymble.shopping.errors import Conflict, ShoppingError
from thymble.shopping.service import utcnow

pytestmark = pytest.mark.db


@pytest.fixture
def stocked(svc, setup):
    lst = setup.list.id
    items = {
        "bananas": svc.add_item(lst, "bananas", store_group_id=setup.everyday.id).item,
        "chips": svc.add_item(lst, "chips", store_id=setup.aldi.id).item,
        "charger": svc.add_item(lst, "phone charger", store_id=setup.walmart.id).item,
        "tp": svc.add_item(lst, "toilet paper", store_id=setup.costco.id, urgent=True).item,
        "olive oil": svc.add_item(lst, "olive oil", store_id=setup.costco.id).item,
        "mystery": svc.add_item(lst, "birthday candles").item,
    }
    svc.upsert_pref(items["olive oil"].ingredient_id, setup.walmart.id, None)
    [items["tp gap"]] = svc.stop_gaps(items["tp"])
    return items


def names(items):
    return sorted(i.display_text for i in items)


def test_in_store_sections(svc, setup, stocked):
    trip = trips.create_trip(svc, setup.walmart.id, TripKind.IN_STORE)
    view = trips.trip_view(svc, trip)
    assert names(view.get_here) == ["bananas", "phone charger"]
    assert names(view.needed_now) == ["toilet paper"]  # the stop-gap, not the bulk item
    assert view.needed_now[0].bridge_of is not None
    assert names(view.could_get_here) == ["birthday candles", "olive oil"]


def test_in_store_check_off_is_bought_here(svc, setup, stocked):
    trip = trips.create_trip(svc, setup.aldi.id, TripKind.IN_STORE)
    trips.check_off(svc, trip, stocked["bananas"])
    assert stocked["bananas"].status == ItemStatus.BOUGHT
    assert stocked["bananas"].trip_id == trip.id
    trips.finish_trip(svc, trip)
    assert trip.status == TripStatus.DONE


def test_pickup_export_confirm_and_missing(svc, setup, stocked):
    trip = trips.create_trip(svc, setup.walmart.id, TripKind.PICKUP)
    suggested = trips.suggested_for_export(svc, trip)
    assert names(suggested) == ["bananas", "phone charger", "toilet paper"]
    svc.upsert_pref(stocked["bananas"].ingredient_id, None, setup.everyday.id,
                    preferred_brand="Great Value", preferred_size="2 lb")  # fmt: skip
    lines = trips.export_order(svc, trip, [i.id for i in suggested])
    by_name = {line.display_text: line for line in lines}
    assert by_name["bananas"].brand == "Great Value" and by_name["toilet paper"].is_stop_gap
    assert trip.status == TripStatus.ON_ORDER
    assert all(i.status == ItemStatus.ON_ORDER for i in suggested)
    assert svc.mirror_text(stocked["charger"]) == "phone charger — on Walmart order"
    # On-order items leave other trips, and show up as "on order" there.
    aldi = trips.trip_view(svc, trips.create_trip(svc, setup.aldi.id, TripKind.IN_STORE))
    assert names(aldi.get_here) == ["chips"] and names(aldi.needed_now) == []
    assert names(aldi.on_order) == ["bananas", "phone charger", "toilet paper"]
    with pytest.raises(Conflict):
        trips.export_order(svc, trip, [stocked["bananas"].id])  # already on order

    trips.confirm_order(svc, trip, missing={stocked["charger"].id})
    assert trip.status == TripStatus.DONE
    assert stocked["bananas"].status == ItemStatus.BOUGHT
    assert stocked["charger"].status == ItemStatus.NEEDED and stocked["charger"].trip_id is None
    # Stop-gap bought: the bulk item stays on the Costco list, no longer urgent.
    assert stocked["tp gap"].status == ItemStatus.BOUGHT
    assert stocked["tp"].status == ItemStatus.NEEDED
    assert stocked["tp"].urgency.value == "normal"


def test_cancel_returns_items(svc, setup, stocked):
    trip = trips.create_trip(svc, setup.walmart.id, TripKind.PICKUP)
    trips.export_order(svc, trip, [stocked["charger"].id])
    trips.cancel_trip(svc, trip)
    assert stocked["charger"].status == ItemStatus.NEEDED


def test_pickup_needs_a_pickup_store(svc, setup):
    with pytest.raises(ShoppingError):
        trips.create_trip(svc, setup.aldi.id, TripKind.PICKUP)


def test_reminders(svc, setup, stocked):
    now = utcnow()
    trip = trips.create_trip(svc, setup.walmart.id, TripKind.PICKUP, now + timedelta(hours=1))
    trips.export_order(svc, trip, [stocked["charger"].id])
    assert trips.due_reminders(svc.session, now) == []
    later = now + timedelta(hours=2)
    assert trips.due_reminders(svc.session, later) == [trip]
    assert trip.status == TripStatus.AWAITING_CONFIRMATION and trip.reminded_at == later
    assert trips.due_reminders(svc.session, later + timedelta(hours=1)) == []
    assert trips.due_reminders(svc.session, later + timedelta(hours=25)) == [trip]
    trips.confirm_order(svc, trip)
    assert trips.due_reminders(svc.session, later + timedelta(days=5)) == []
