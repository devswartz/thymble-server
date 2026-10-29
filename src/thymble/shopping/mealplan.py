"""Meal plan -> shopping list (architecture §6.1).

Plan entries expand to recipe ingredients, scaled to the entry's servings, merged into
open items by ingredient + compatible unit. Each contribution is a
`shopping_item_source` row, so removing a recipe (or plan entry) subtracts its share.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select

from thymble.models import (
    Ingredient,
    ItemSourceType,
    ItemStatus,
    MealPlanEntry,
    Recipe,
    RecipeIngredient,
    ShoppingItem,
    ShoppingItemSource,
)
from thymble.shopping import units
from thymble.shopping.errors import NotFound, ShoppingError
from thymble.shopping.service import ShoppingService, utcnow


def _scale(recipe: Recipe, servings: Decimal | None) -> Decimal:
    if servings and recipe.yield_quantity and recipe.yield_quantity > 0:
        return Decimal(servings) / recipe.yield_quantity
    return Decimal(1)


def _live_sources(svc: ShoppingService, list_id: uuid.UUID, *conditions):
    return list(
        svc.session.scalars(
            select(ShoppingItemSource)
            .join(ShoppingItem, ShoppingItem.id == ShoppingItemSource.shopping_item_id)
            .where(
                ShoppingItem.list_id == list_id,
                ShoppingItemSource.household_id == svc.household_id,
                ShoppingItemSource.deleted_at.is_(None),
                *conditions,
            )
        )
    )


def _add_recipe_lines(
    svc: ShoppingService,
    list_id: uuid.UUID,
    recipe: Recipe,
    scale: Decimal,
    source_type: ItemSourceType,
    entry_id: uuid.UUID | None,
) -> list[ShoppingItem]:
    lines = svc.session.scalars(
        select(RecipeIngredient)
        .where(RecipeIngredient.recipe_id == recipe.id, RecipeIngredient.deleted_at.is_(None))
        .order_by(RecipeIngredient.position)
    ).all()
    items = []
    for line in lines:
        base = line.quantity_max if line.quantity_max is not None else line.quantity
        quantity = (base * scale).quantize(units.QUANTUM) if base is not None else None
        ingredient = svc.session.get(Ingredient, line.ingredient_id) if line.ingredient_id else None
        if ingredient is not None:
            result = svc.add_item(
                list_id,
                ingredient.name,
                quantity,
                line.unit,
                ingredient_id=ingredient.id,
                source_type=source_type,
                recipe_id=recipe.id,
                meal_plan_entry_id=entry_id,
            )
        else:
            # Not linked to the catalog yet: keep the original line as free text.
            result = svc.add_item(
                list_id,
                line.raw_text,
                ingredient_id=None,
                resolve=False,
                source_type=source_type,
                recipe_id=recipe.id,
                meal_plan_entry_id=entry_id,
            )
        items.append(result.item)
    return items


def add_meal_plan(
    svc: ShoppingService,
    list_id: uuid.UUID,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    entry_ids: list[uuid.UUID] | None = None,
) -> list[ShoppingItem]:
    """'Add week to list'. Entries already on the list are skipped (idempotent)."""
    svc.get_list(list_id)
    q = select(MealPlanEntry).where(
        MealPlanEntry.household_id == svc.household_id,
        MealPlanEntry.deleted_at.is_(None),
        MealPlanEntry.recipe_id.is_not(None),
    )
    if entry_ids is not None:
        q = q.where(MealPlanEntry.id.in_(entry_ids))
    elif start is not None and end is not None:
        q = q.where(MealPlanEntry.date >= start, MealPlanEntry.date <= end)
    else:
        raise ShoppingError("give a date range or entry ids")
    items: dict[uuid.UUID, ShoppingItem] = {}
    for entry in svc.session.scalars(q.order_by(MealPlanEntry.date, MealPlanEntry.id)):
        if _live_sources(svc, list_id, ShoppingItemSource.meal_plan_entry_id == entry.id):
            continue
        recipe = svc.session.get(Recipe, entry.recipe_id)
        if recipe is None or recipe.deleted_at is not None:
            continue
        for item in _add_recipe_lines(
            svc,
            list_id,
            recipe,
            _scale(recipe, entry.servings),
            ItemSourceType.MEAL_PLAN_ENTRY,
            entry.id,
        ):
            items[item.id] = item
    return list(items.values())


def add_recipe(
    svc: ShoppingService, list_id: uuid.UUID, recipe_id: uuid.UUID, servings: Decimal | None
) -> list[ShoppingItem]:
    """Add one recipe outside the meal plan. Re-adding replaces the earlier share."""
    svc.get_list(list_id)
    recipe = svc.session.get(Recipe, recipe_id)
    if recipe is None or recipe.household_id != svc.household_id or recipe.deleted_at:
        raise NotFound(f"recipe {recipe_id} not found")
    remove_recipe(svc, list_id, recipe_id)
    return _add_recipe_lines(
        svc, list_id, recipe, _scale(recipe, servings), ItemSourceType.RECIPE, None
    )


def remove_meal_plan_entry(svc: ShoppingService, list_id: uuid.UUID, entry_id: uuid.UUID) -> int:
    sources = _live_sources(svc, list_id, ShoppingItemSource.meal_plan_entry_id == entry_id)
    _remove_sources(svc, sources)
    return len(sources)


def remove_recipe(svc: ShoppingService, list_id: uuid.UUID, recipe_id: uuid.UUID) -> int:
    sources = _live_sources(
        svc,
        list_id,
        ShoppingItemSource.recipe_id == recipe_id,
        ShoppingItemSource.source_type == ItemSourceType.RECIPE,
    )
    _remove_sources(svc, sources)
    return len(sources)


def _remove_sources(svc: ShoppingService, sources: list[ShoppingItemSource]) -> None:
    """Subtract each source's share; delete items nothing else asked for."""
    now = utcnow()
    for source in sources:
        source.deleted_at = now
    svc.session.flush()
    for item_id in {s.shopping_item_id for s in sources}:
        item = svc.session.get(ShoppingItem, item_id)
        if item is None or item.deleted_at is not None or item.status != ItemStatus.NEEDED:
            continue  # already bought / on order: the share was used, leave it
        mine = [s for s in sources if s.shopping_item_id == item_id]
        remaining = svc.session.scalars(
            select(ShoppingItemSource).where(
                ShoppingItemSource.shopping_item_id == item_id,
                ShoppingItemSource.deleted_at.is_(None),
            )
        ).all()
        if not remaining:
            svc.delete_item(item)
            continue
        if item.quantity is not None:
            for s in mine:
                if s.quantity is None:
                    continue
                share = units.convert(s.quantity, s.unit, item.unit)
                if share is not None:
                    item.quantity -= share
            if item.quantity <= 0:
                item.quantity, item.unit = None, None
            else:
                item.quantity = item.quantity.quantize(units.QUANTUM)
        svc._touch(item)
