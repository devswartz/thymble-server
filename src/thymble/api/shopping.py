"""Shopping lists and items, meal plan -> list, store suggestions (architecture §6)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from thymble.api.deps import DbSession, Household
from thymble.api.shopping_schemas import (
    CheckIn,
    DismissIn,
    ItemIn,
    ItemOut,
    ItemPatch,
    ItemResult,
    ListIn,
    ListOut,
    ListPatch,
    MealPlanIn,
    RecipeIn,
    SuggestionOut,
)
from thymble.models import ItemStatus, ShoppingList
from thymble.shopping import mealplan
from thymble.shopping.errors import ShoppingError
from thymble.shopping.service import ShoppingService, utcnow
from thymble.shopping.text import parse_entry

router = APIRouter(prefix="/shopping", tags=["shopping"])


def service(db: DbSession, hh: Household) -> ShoppingService:
    return ShoppingService(db, hh.household_id, hh.user_id)


def _result(svc: ShoppingService, item, merged: bool = False, stop_gap=None) -> ItemResult:
    return ItemResult(
        item=ItemOut.model_validate(item),
        merged=merged,
        stop_gap=ItemOut.model_validate(stop_gap) if stop_gap is not None else None,
        prompts=svc.prompts,
        suggestions=[
            SuggestionOut.model_validate(s, from_attributes=True) for s in svc.suggestions
        ],
    )


# ------------------------------------------------------------------ lists
@router.get("/lists")
def list_lists(db: DbSession, hh: Household) -> list[ListOut]:
    rows = db.scalars(
        select(ShoppingList)
        .where(ShoppingList.household_id == hh.household_id, ShoppingList.deleted_at.is_(None))
        .order_by(ShoppingList.created_at)
    )
    return [ListOut.model_validate(r) for r in rows]


@router.post("/lists", status_code=status.HTTP_201_CREATED)
def create_list(body: ListIn, db: DbSession, hh: Household) -> ListOut:
    lst = ShoppingList(household_id=hh.household_id, name=body.name, is_active=body.is_active)
    db.add(lst)
    db.commit()
    return ListOut.model_validate(lst)


@router.patch("/lists/{list_id}")
def update_list(list_id: uuid.UUID, body: ListPatch, db: DbSession, hh: Household) -> ListOut:
    svc = service(db, hh)
    lst = svc.get_list(list_id)
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(lst, key, value)
    db.commit()
    return ListOut.model_validate(lst)


@router.delete("/lists/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_list(list_id: uuid.UUID, db: DbSession, hh: Household) -> None:
    svc = service(db, hh)
    lst = svc.get_list(list_id)
    for item in svc.items(list_id):
        svc.delete_item(item)
    lst.deleted_at = utcnow()
    db.commit()


@router.get("/lists/{list_id}/items")
def list_items(
    list_id: uuid.UUID, db: DbSession, hh: Household, status: ItemStatus | None = None
) -> list[ItemOut]:
    svc = service(db, hh)
    svc.get_list(list_id)
    items = svc.items(list_id, [status] if status else None)
    return [ItemOut.model_validate(i) for i in items]


@router.post("/lists/{list_id}/items", status_code=status.HTTP_201_CREATED)
def add_item(list_id: uuid.UUID, body: ItemIn, db: DbSession, hh: Household) -> ItemResult:
    """Add (or merge into) an item. The item's home store is remembered for next time."""
    svc = service(db, hh)
    store_id, remember = body.store_id, body.remember_store
    if body.text:
        p = parse_entry(body.text, svc.store_names())
        name, quantity, unit, urgent = p.name, p.quantity, p.unit, p.urgent or body.urgent
        hinted = svc.store_for_hint(p.store_hint)
        if hinted is not None and store_id is None and body.store_group_id is None:
            # "bread from aldi": assign it there; it becomes the home only if it has none.
            store_id = hinted.id
            known = svc.resolve_ingredient(name)
            remember = remember and (known is None or svc.home_of(known.id) is None)
    elif body.name:
        name, quantity, unit, urgent = body.name, body.quantity, body.unit, body.urgent
    else:
        raise ShoppingError("give text or name")
    result = svc.add_item(
        list_id,
        name,
        quantity,
        unit,
        store_id=store_id,
        store_group_id=body.store_group_id,
        remember=remember,
        urgent=urgent,
    )
    gaps = svc.stop_gaps(result.item)
    db.commit()
    return _result(svc, result.item, result.merged, gaps[0] if gaps else None)


# ------------------------------------------------------------------ items
@router.get("/items/{item_id}")
def get_item(item_id: uuid.UUID, db: DbSession, hh: Household) -> ItemOut:
    return ItemOut.model_validate(service(db, hh).get_item(item_id))


@router.patch("/items/{item_id}")
def update_item(item_id: uuid.UUID, body: ItemPatch, db: DbSession, hh: Household) -> ItemResult:
    svc = service(db, hh)
    item = svc.get_item(item_id)
    svc.update_item(
        item,
        display_text=body.display_text,
        quantity=body.quantity,
        unit=body.unit,
        clear_quantity=body.clear_quantity,
        aisle=body.aisle,
    )
    if body.assign:
        svc.assign(item, body.store_id, body.store_group_id, remember=body.remember_store)
    gap = None
    if body.urgent is not None:
        gap = svc.set_urgent(item, body.urgent)
    db.commit()
    return _result(svc, item, stop_gap=gap)


@router.post("/items/{item_id}/check")
def check_item(
    item_id: uuid.UUID, db: DbSession, hh: Household, body: CheckIn | None = None
) -> ItemResult:
    """Bought. With `store_id`, the purchase teaches where the item can be bought."""
    body = body or CheckIn()
    svc = service(db, hh)
    item = svc.get_item(item_id)
    store_id = body.store_id
    if body.trip_id is not None:
        store_id = store_id or svc.get_trip(body.trip_id).store_id
    elif store_id is not None:
        svc.get_store(store_id)
    svc.mark_bought(item, store_id=store_id, trip_id=body.trip_id)
    db.commit()
    return _result(svc, item)


@router.post("/items/{item_id}/uncheck")
def uncheck_item(item_id: uuid.UUID, db: DbSession, hh: Household) -> ItemOut:
    svc = service(db, hh)
    item = svc.mark_needed(svc.get_item(item_id))
    db.commit()
    return ItemOut.model_validate(item)


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: uuid.UUID, db: DbSession, hh: Household) -> None:
    svc = service(db, hh)
    svc.delete_item(svc.get_item(item_id))
    db.commit()


# ------------------------------------------------------- meal plan / recipes
@router.post("/lists/{list_id}/meal-plan")
def add_meal_plan(
    list_id: uuid.UUID, body: MealPlanIn, db: DbSession, hh: Household
) -> list[ItemOut]:
    """'Add week to list': scaled, merged recipe ingredients for the plan entries."""
    svc = service(db, hh)
    items = mealplan.add_meal_plan(
        svc, list_id, start=body.start, end=body.end, entry_ids=body.entry_ids
    )
    db.commit()
    return [ItemOut.model_validate(i) for i in items]


@router.delete("/lists/{list_id}/meal-plan/{entry_id}")
def remove_meal_plan_entry(
    list_id: uuid.UUID, entry_id: uuid.UUID, db: DbSession, hh: Household
) -> dict[str, int]:
    """Subtract a plan entry's share (e.g. the recipe was removed from the plan)."""
    svc = service(db, hh)
    svc.get_list(list_id)
    removed = mealplan.remove_meal_plan_entry(svc, list_id, entry_id)
    db.commit()
    return {"sources_removed": removed}


@router.post("/lists/{list_id}/recipes")
def add_recipe(list_id: uuid.UUID, body: RecipeIn, db: DbSession, hh: Household) -> list[ItemOut]:
    svc = service(db, hh)
    items = mealplan.add_recipe(svc, list_id, body.recipe_id, body.servings)
    db.commit()
    return [ItemOut.model_validate(i) for i in items]


@router.delete("/lists/{list_id}/recipes/{recipe_id}")
def remove_recipe(
    list_id: uuid.UUID, recipe_id: uuid.UUID, db: DbSession, hh: Household
) -> dict[str, int]:
    svc = service(db, hh)
    svc.get_list(list_id)
    removed = mealplan.remove_recipe(svc, list_id, recipe_id)
    db.commit()
    return {"sources_removed": removed}


# ------------------------------------------------------------ suggestions
@router.get("/suggestions")
def home_suggestions(db: DbSession, hh: Household) -> list[SuggestionOut]:
    """'Make this the usual store?' — items bought elsewhere 3+ times since their home
    was set. Accept with PUT /v1/item-prefs/{ingredient_id}/home."""
    svc = service(db, hh)
    return [
        SuggestionOut.model_validate(s, from_attributes=True)
        for s in svc.pending_home_suggestions()
    ]


@router.post("/suggestions/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_suggestion(body: DismissIn, db: DbSession, hh: Household) -> None:
    svc = service(db, hh)
    svc.dismiss_home_suggestion(body.ingredient_id, body.store_id)
    db.commit()


# ------------------------------------------------------------ registration
async def _shopping_error(request: Request, exc: ShoppingError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)


def register(app: FastAPI, prefix: str = "/v1") -> None:
    """Mount the shopping, store, trip and connector routers on the app."""
    from thymble.api import connectors, stores, trips

    app.add_exception_handler(ShoppingError, _shopping_error)
    for r in (router, stores.router, trips.router, connectors.router):
        app.include_router(r, prefix=prefix)
