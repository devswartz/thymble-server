"""Trips: in-store views, pickup-order export and confirmation (§6.4, D-030)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import select

from thymble.api.deps import DbSession, Household
from thymble.api.shopping import _result, service
from thymble.api.shopping_schemas import (
    ConfirmIn,
    ExportIn,
    ExportLineOut,
    ExportOut,
    ItemOut,
    ItemResult,
    StoreOut,
    TripIn,
    TripOut,
    TripViewOut,
)
from thymble.models import Trip, TripStatus
from thymble.shopping import trips

router = APIRouter(prefix="/trips", tags=["trips"])


def _items(items) -> list[ItemOut]:
    return [ItemOut.model_validate(i) for i in items]


@router.get("")
def list_trips(db: DbSession, hh: Household, status: TripStatus | None = None) -> list[TripOut]:
    q = select(Trip).where(Trip.household_id == hh.household_id, Trip.deleted_at.is_(None))
    if status is not None:
        q = q.where(Trip.status == status)
    return [TripOut.model_validate(t) for t in db.scalars(q.order_by(Trip.created_at.desc()))]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_trip(body: TripIn, db: DbSession, hh: Household) -> TripOut:
    """ "I'm at Aldi" (in_store) or "Export to Walmart" (pickup / delivery)."""
    svc = service(db, hh)
    trip = trips.create_trip(svc, body.store_id, body.kind, body.expected_pickup_at)
    db.commit()
    return TripOut.model_validate(trip)


@router.get("/{trip_id}")
def get_trip(trip_id: uuid.UUID, db: DbSession, hh: Household) -> TripViewOut:
    """The trip with its sections: get here / needed now / could get here, plus items
    on other orders and the items on this order."""
    svc = service(db, hh)
    trip = svc.get_trip(trip_id)
    view = trips.trip_view(svc, trip)
    return TripViewOut(
        trip=TripOut.model_validate(trip),
        store=StoreOut.model_validate(view.store),
        get_here=_items(view.get_here),
        needed_now=_items(view.needed_now),
        could_get_here=_items(view.could_get_here),
        on_order=_items(view.on_order),
        order_items=_items(trips.order_items(svc, trip)),
    )


@router.post("/{trip_id}/items/{item_id}/check")
def check_item(trip_id: uuid.UUID, item_id: uuid.UUID, db: DbSession, hh: Household) -> ItemResult:
    svc = service(db, hh)
    item = trips.check_off(svc, svc.get_trip(trip_id), svc.get_item(item_id))
    db.commit()
    return _result(svc, item)


@router.get("/{trip_id}/suggested")
def suggested(trip_id: uuid.UUID, db: DbSession, hh: Household) -> list[ItemOut]:
    """Suggested items for a pickup order: this store's items plus stop-gaps/urgent."""
    svc = service(db, hh)
    return _items(trips.suggested_for_export(svc, svc.get_trip(trip_id)))


@router.post("/{trip_id}/export")
def export(trip_id: uuid.UUID, body: ExportIn, db: DbSession, hh: Household) -> ExportOut:
    """Items -> on order (pending). Returns the lines a retailer connector turns into a
    cart link (retailer connectors are not built yet)."""
    svc = service(db, hh)
    trip = svc.get_trip(trip_id)
    lines = trips.export_order(svc, trip, body.item_ids)
    db.commit()
    return ExportOut(
        trip=TripOut.model_validate(trip),
        lines=[ExportLineOut.model_validate(line) for line in lines],
    )


@router.post("/{trip_id}/confirm")
def confirm(trip_id: uuid.UUID, body: ConfirmIn, db: DbSession, hh: Household) -> TripOut:
    """'Got everything?' Empty `missing_item_ids` = confirm all."""
    svc = service(db, hh)
    trip = svc.get_trip(trip_id)
    trips.confirm_order(svc, trip, set(body.missing_item_ids))
    db.commit()
    return TripOut.model_validate(trip)


@router.post("/{trip_id}/finish")
def finish(trip_id: uuid.UUID, db: DbSession, hh: Household) -> TripOut:
    svc = service(db, hh)
    trip = trips.finish_trip(svc, svc.get_trip(trip_id))
    db.commit()
    return TripOut.model_validate(trip)


@router.post("/{trip_id}/cancel")
def cancel(trip_id: uuid.UUID, db: DbSession, hh: Household) -> TripOut:
    svc = service(db, hh)
    trip = trips.cancel_trip(svc, svc.get_trip(trip_id))
    db.commit()
    return TripOut.model_validate(trip)
