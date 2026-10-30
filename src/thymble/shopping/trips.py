"""Trips: in-store views, pickup-order export and confirmation (§6.4, D-030)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session

from thymble.models import (
    ItemStatus,
    ItemStorePref,
    ShoppingItem,
    ShoppingList,
    Store,
    Trip,
    TripKind,
    TripStatus,
    Urgency,
)
from thymble.shopping.errors import Conflict, ShoppingError
from thymble.shopping.service import ShoppingService, utcnow

REMINDER_REPEAT = timedelta(hours=24)


@dataclass
class TripView:
    trip: Trip
    store: Store
    get_here: list[ShoppingItem] = field(default_factory=list)
    needed_now: list[ShoppingItem] = field(default_factory=list)
    could_get_here: list[ShoppingItem] = field(default_factory=list)
    # Items on other pickup orders, so nobody buys them twice.
    on_order: list[ShoppingItem] = field(default_factory=list)


@dataclass
class ExportLine:
    """One cart line for a retailer connector to turn into a cart link (D-026)."""

    item_id: uuid.UUID
    display_text: str
    quantity: Decimal | None
    unit: str | None
    is_stop_gap: bool
    search_terms: str | None
    brand: str | None
    size: str | None
    note: str | None


def create_trip(
    svc: ShoppingService,
    store_id: uuid.UUID,
    kind: TripKind,
    expected_pickup_at: datetime | None = None,
) -> Trip:
    store = svc.get_store(store_id)
    if kind == TripKind.PICKUP and not store.supports_pickup:
        raise ShoppingError(f"{store.name} is not set up for pickup orders")
    if kind == TripKind.DELIVERY and not store.supports_delivery:
        raise ShoppingError(f"{store.name} is not set up for delivery")
    trip = Trip(
        household_id=svc.household_id,
        store_id=store_id,
        kind=kind,
        status=TripStatus.PLANNING,
        user_id=svc.user_id,
        expected_pickup_at=expected_pickup_at,
    )
    svc.session.add(trip)
    svc.session.flush()
    return trip


def _household_items(svc: ShoppingService, statuses: list[ItemStatus]) -> list[ShoppingItem]:
    return list(
        svc.session.scalars(
            select(ShoppingItem)
            .join(ShoppingList, ShoppingList.id == ShoppingItem.list_id)
            .where(
                ShoppingItem.household_id == svc.household_id,
                ShoppingItem.deleted_at.is_(None),
                ShoppingItem.status.in_(statuses),
                ShoppingList.deleted_at.is_(None),
                ShoppingList.is_active.is_(True),
            )
            .order_by(ShoppingItem.aisle.nulls_last(), ShoppingItem.created_at)
        )
    )


def trip_view(svc: ShoppingService, trip: Trip) -> TripView:
    store = svc.get_store(trip.store_id)
    groups = svc.groups_containing(store.id)
    ok_here = set(
        svc.session.scalars(
            select(ItemStorePref.ingredient_id).where(
                ItemStorePref.household_id == svc.household_id,
                ItemStorePref.deleted_at.is_(None),
                or_(
                    ItemStorePref.store_id == store.id,
                    ItemStorePref.store_group_id.in_(groups) if groups else false(),
                ),
            )
        )
    )
    view = TripView(trip=trip, store=store)
    needed = _household_items(svc, [ItemStatus.NEEDED])
    on_order = _household_items(svc, [ItemStatus.ON_ORDER])

    def is_here(item: ShoppingItem) -> bool:
        return item.store_id == store.id or (
            item.store_group_id is not None and item.store_group_id in groups
        )

    # Urgent bulk items whose short-term need is covered by an open stop-gap.
    bridged = {i.bridge_of for i in needed + on_order if i.bridge_of is not None}
    # Bulk items this trip can buy (D-029: shopping at the bulk store itself). Buying one
    # clears its stop-gaps too, so here the bulk item is needed now and its stop-gaps hide.
    bulk_here = {i.id for i in needed if i.bridge_of is None and is_here(i)}
    for item in needed:
        if item.bridge_of is not None and item.bridge_of in bulk_here:
            continue
        here = is_here(item)
        urgent = item.bridge_of is not None or (
            item.urgency == Urgency.URGENT and (item.id not in bridged or item.id in bulk_here)
        )
        if urgent:
            view.needed_now.append(item)
        elif here:
            view.get_here.append(item)
        elif (item.ingredient_id in ok_here and item.ingredient_id is not None) or (
            item.store_id is None and item.store_group_id is None
        ):
            view.could_get_here.append(item)
    view.on_order = [i for i in on_order if i.trip_id != trip.id]
    return view


def suggested_for_export(svc: ShoppingService, trip: Trip) -> list[ShoppingItem]:
    """Pickup suggestion: this store's and its groups' items, plus stop-gaps / urgent."""
    view = trip_view(svc, trip)
    return view.get_here + view.needed_now


def _lines(svc: ShoppingService, trip: Trip, items: list[ShoppingItem]) -> list[ExportLine]:
    groups = svc.groups_containing(trip.store_id)
    lines = []
    for item in items:
        pref = None
        if item.ingredient_id is not None:
            pref = svc.pref_for(item.ingredient_id, trip.store_id, None)
            for gid in groups:
                if pref is None:
                    pref = svc.pref_for(item.ingredient_id, None, gid)
        lines.append(
            ExportLine(
                item_id=item.id,
                display_text=item.display_text,
                quantity=item.quantity,
                unit=item.unit,
                is_stop_gap=item.bridge_of is not None,
                search_terms=pref.preferred_search_terms if pref else None,
                brand=pref.preferred_brand if pref else None,
                size=pref.preferred_size if pref else None,
                note=pref.note if pref else None,
            )
        )
    return lines


def export_order(svc: ShoppingService, trip: Trip, item_ids: list[uuid.UUID]) -> list[ExportLine]:
    """Put items on this pickup/delivery order: they become on order (pending) and
    leave other trips (D-030). Returns the cart lines for a retailer connector."""
    if trip.kind == TripKind.IN_STORE:
        raise ShoppingError("in-store trips are not exported")
    if trip.status not in (TripStatus.PLANNING, TripStatus.ON_ORDER):
        raise Conflict(f"trip is {trip.status.value}")
    items = []
    for item_id in dict.fromkeys(item_ids):
        item = svc.get_item(item_id)
        if item.status != ItemStatus.NEEDED:
            raise Conflict(f"item {item_id} is {item.status.value}, not needed")
        items.append(item)
    for item in items:
        item.status = ItemStatus.ON_ORDER
        item.trip_id = trip.id
        svc._touch(item)
    trip.status = TripStatus.ON_ORDER
    trip.user_id = trip.user_id or svc.user_id
    svc.session.flush()
    return _lines(svc, trip, items)


def order_items(svc: ShoppingService, trip: Trip) -> list[ShoppingItem]:
    return list(
        svc.session.scalars(
            select(ShoppingItem).where(
                ShoppingItem.trip_id == trip.id,
                ShoppingItem.household_id == svc.household_id,
                ShoppingItem.deleted_at.is_(None),
                ShoppingItem.status == ItemStatus.ON_ORDER,
            )
        )
    )


def confirm_order(
    svc: ShoppingService, trip: Trip, missing: set[uuid.UUID] | frozenset = frozenset()
) -> list[ShoppingItem]:
    """'Got everything?': got -> bought here; missing -> back on the list as needed."""
    if trip.status not in (TripStatus.ON_ORDER, TripStatus.AWAITING_CONFIRMATION):
        raise Conflict(f"trip is {trip.status.value}")
    items = order_items(svc, trip)
    for item in items:
        if item.id in missing:
            svc.mark_needed(item)
        else:
            svc.mark_bought(item, store_id=trip.store_id, trip_id=trip.id)
    trip.status = TripStatus.DONE
    trip.completed_at = utcnow()
    svc.session.flush()
    return items


def check_off(svc: ShoppingService, trip: Trip, item: ShoppingItem) -> ShoppingItem:
    """In-store check-off = bought at this trip's store."""
    if trip.status in (TripStatus.DONE, TripStatus.CANCELLED):
        raise Conflict(f"trip is {trip.status.value}")
    return svc.mark_bought(item, store_id=trip.store_id, trip_id=trip.id)


def finish_trip(svc: ShoppingService, trip: Trip) -> Trip:
    if trip.kind != TripKind.IN_STORE:
        raise ShoppingError("pickup and delivery orders finish by confirmation")
    trip.status = TripStatus.DONE
    trip.completed_at = utcnow()
    svc.session.flush()
    return trip


def cancel_trip(svc: ShoppingService, trip: Trip) -> Trip:
    if trip.status in (TripStatus.DONE, TripStatus.CANCELLED):
        raise Conflict(f"trip is {trip.status.value}")
    for item in order_items(svc, trip):
        svc.mark_needed(item)
    trip.status = TripStatus.CANCELLED
    svc.session.flush()
    return trip


def due_reminders(session: Session, now: datetime | None = None) -> list[Trip]:
    """Worker job body: orders past their pickup time await confirmation; unconfirmed
    orders get a reminder, repeated daily. Returns the trips to remind about."""
    now = now or utcnow()
    due = []
    for trip in session.scalars(
        select(Trip).where(
            Trip.deleted_at.is_(None),
            Trip.status == TripStatus.ON_ORDER,
            Trip.expected_pickup_at.is_not(None),
            Trip.expected_pickup_at <= now,
        )
    ):
        trip.status = TripStatus.AWAITING_CONFIRMATION
        trip.reminded_at = now
        due.append(trip)
    for trip in session.scalars(
        select(Trip).where(
            Trip.deleted_at.is_(None),
            Trip.status == TripStatus.AWAITING_CONFIRMATION,
            or_(Trip.reminded_at.is_(None), Trip.reminded_at <= now - REMINDER_REPEAT),
        )
    ):
        if trip in due:
            continue
        trip.reminded_at = now
        due.append(trip)
    session.flush()
    return due
