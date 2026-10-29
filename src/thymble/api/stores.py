"""Stores, store groups and per-item store preferences (§6.4, D-027/D-028)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import select

from thymble.api.deps import DbSession, Household
from thymble.api.shopping import service
from thymble.api.shopping_schemas import (
    GroupIn,
    GroupOut,
    GroupPatch,
    HomeIn,
    PrefIn,
    PrefOut,
    StoreIn,
    StoreOut,
    StorePatch,
)
from thymble.models import ConnectorAccount, ItemStorePref, Store, StoreGroup, StoreGroupMember
from thymble.shopping.errors import NotFound, ShoppingError
from thymble.shopping.service import ShoppingService, utcnow
from thymble.shopping.units import normalize_unit

router = APIRouter(tags=["stores"])


def _check_external_list(svc: ShoppingService, store: Store) -> None:
    if (store.external_list_connector_account_id is None) != (store.external_list_id is None):
        raise ShoppingError("a linked list needs both the connector account and the list id")
    if store.external_list_connector_account_id is not None:
        account = svc.session.get(ConnectorAccount, store.external_list_connector_account_id)
        if account is None or account.household_id != svc.household_id or account.deleted_at:
            raise NotFound("connector account not found")


# ------------------------------------------------------------------ stores
@router.get("/stores")
def list_stores(db: DbSession, hh: Household) -> list[StoreOut]:
    rows = db.scalars(
        select(Store)
        .where(Store.household_id == hh.household_id, Store.deleted_at.is_(None))
        .order_by(Store.name)
    )
    return [StoreOut.model_validate(r) for r in rows]


@router.post("/stores", status_code=status.HTTP_201_CREATED)
def create_store(body: StoreIn, db: DbSession, hh: Household) -> StoreOut:
    svc = service(db, hh)
    store = Store(household_id=hh.household_id, **body.model_dump())
    _check_external_list(svc, store)
    db.add(store)
    db.commit()
    return StoreOut.model_validate(store)


@router.patch("/stores/{store_id}")
def update_store(store_id: uuid.UUID, body: StorePatch, db: DbSession, hh: Household) -> StoreOut:
    svc = service(db, hh)
    store = svc.get_store(store_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(store, key, value)
    _check_external_list(svc, store)
    db.commit()
    return StoreOut.model_validate(store)


@router.delete("/stores/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_store(store_id: uuid.UUID, db: DbSession, hh: Household) -> None:
    svc = service(db, hh)
    store = svc.get_store(store_id)
    now = utcnow()
    store.deleted_at = now
    for row in db.scalars(
        select(StoreGroupMember).where(
            StoreGroupMember.store_id == store_id, StoreGroupMember.deleted_at.is_(None)
        )
    ):
        row.deleted_at = now
    db.commit()


# ------------------------------------------------------------------ groups
def _group_out(svc: ShoppingService, group: StoreGroup) -> GroupOut:
    return GroupOut(
        id=group.id,
        name=group.name,
        is_default=group.is_default,
        store_ids=sorted(svc.group_store_ids(group.id)),
    )


def _set_members(svc: ShoppingService, group: StoreGroup, store_ids: list[uuid.UUID]) -> None:
    for sid in store_ids:
        svc.get_store(sid)
    wanted = set(store_ids)
    rows = svc.session.scalars(
        select(StoreGroupMember).where(
            StoreGroupMember.store_group_id == group.id, StoreGroupMember.deleted_at.is_(None)
        )
    ).all()
    for row in rows:
        if row.store_id in wanted:
            wanted.discard(row.store_id)
        else:
            row.deleted_at = utcnow()
    for sid in store_ids:
        if sid in wanted:
            svc.session.add(
                StoreGroupMember(
                    household_id=svc.household_id, store_group_id=group.id, store_id=sid
                )
            )
            wanted.discard(sid)
    svc.session.flush()


def _make_default(svc: ShoppingService, group: StoreGroup) -> None:
    for other in svc.session.scalars(
        select(StoreGroup).where(
            StoreGroup.household_id == svc.household_id,
            StoreGroup.is_default.is_(True),
            StoreGroup.id != group.id,
        )
    ):
        other.is_default = False


@router.get("/store-groups")
def list_groups(db: DbSession, hh: Household) -> list[GroupOut]:
    svc = service(db, hh)
    rows = db.scalars(
        select(StoreGroup)
        .where(StoreGroup.household_id == hh.household_id, StoreGroup.deleted_at.is_(None))
        .order_by(StoreGroup.name)
    )
    return [_group_out(svc, g) for g in rows]


@router.post("/store-groups", status_code=status.HTTP_201_CREATED)
def create_group(body: GroupIn, db: DbSession, hh: Household) -> GroupOut:
    svc = service(db, hh)
    group = StoreGroup(household_id=hh.household_id, name=body.name, is_default=body.is_default)
    db.add(group)
    db.flush()
    if body.is_default:
        _make_default(svc, group)
    _set_members(svc, group, body.store_ids)
    db.commit()
    return _group_out(svc, group)


@router.patch("/store-groups/{group_id}")
def update_group(group_id: uuid.UUID, body: GroupPatch, db: DbSession, hh: Household) -> GroupOut:
    svc = service(db, hh)
    group = svc.get_group(group_id)
    if body.name is not None:
        group.name = body.name
    if body.is_default is not None:
        group.is_default = body.is_default
        if body.is_default:
            _make_default(svc, group)
    if body.store_ids is not None:
        _set_members(svc, group, body.store_ids)
    db.commit()
    return _group_out(svc, group)


@router.delete("/store-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(group_id: uuid.UUID, db: DbSession, hh: Household) -> None:
    svc = service(db, hh)
    group = svc.get_group(group_id)
    _set_members(svc, group, [])
    group.deleted_at = utcnow()
    db.commit()


# ------------------------------------------------------------ item prefs
@router.get("/item-prefs")
def list_prefs(
    db: DbSession, hh: Household, ingredient_id: uuid.UUID | None = None
) -> list[PrefOut]:
    q = select(ItemStorePref).where(
        ItemStorePref.household_id == hh.household_id, ItemStorePref.deleted_at.is_(None)
    )
    if ingredient_id is not None:
        q = q.where(ItemStorePref.ingredient_id == ingredient_id)
    return [PrefOut.model_validate(p) for p in db.scalars(q.order_by(ItemStorePref.created_at))]


@router.put("/item-prefs")
def upsert_pref(body: PrefIn, db: DbSession, hh: Household) -> PrefOut:
    """Per-store details (also-OK store, brand / size / note, typical quantity)."""
    svc = service(db, hh)
    details = body.model_dump(
        exclude={"ingredient_id", "store_id", "store_group_id"}, exclude_unset=True
    )
    if "typical_unit" in details:
        details["typical_unit"] = normalize_unit(details["typical_unit"])
    pref = svc.upsert_pref(body.ingredient_id, body.store_id, body.store_group_id, **details)
    db.commit()
    return PrefOut.model_validate(pref)


@router.put("/item-prefs/{ingredient_id}/home")
def set_home(
    ingredient_id: uuid.UUID, body: HomeIn, db: DbSession, hh: Household
) -> PrefOut | None:
    """Set (or clear, with both null) the item's home store or group."""
    svc = service(db, hh)
    pref = svc.set_home(ingredient_id, body.store_id, body.store_group_id)
    db.commit()
    return PrefOut.model_validate(pref) if pref else None


@router.delete("/item-prefs/{pref_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pref(pref_id: uuid.UUID, db: DbSession, hh: Household) -> None:
    svc = service(db, hh)
    pref = svc._get(ItemStorePref, pref_id)
    pref.deleted_at = utcnow()
    db.commit()
