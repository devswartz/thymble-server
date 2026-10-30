"""Meal plan -> ingredients -> shopping list (architecture §6.1).

Plan entries expand to recipe ingredients, scaled to the entry's servings, merged by
ingredient + compatible unit (the list's merge rule, `thymble.units.mergeable`).
`expand_plan` returns that merged list without touching the shopping list;
`add_meal_plan` puts it on a list, where each contribution is a `shopping_item_source`
row, so removing a recipe (or plan entry) subtracts its share again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from thymble import units
from thymble.models import (
    Ingredient,
    ItemSourceType,
    ItemStatus,
    MealPlanEntry,
    MealSlot,
    Recipe,
    RecipeIngredient,
    ShoppingItem,
    ShoppingItemSource,
)
from thymble.shopping.errors import NotFound, ShoppingError
from thymble.shopping.service import ShoppingService, utcnow


def scale_factor(recipe: Recipe, servings: Decimal | None) -> Decimal:
    """Servings asked for / the recipe's yield; 1 when either is missing."""
    if servings and recipe.yield_quantity and recipe.yield_quantity > 0:
        return Decimal(servings) / recipe.yield_quantity
    return Decimal(1)


def _scaled(value: Decimal | None, scale: Decimal) -> Decimal | None:
    return (value * scale).quantize(units.QUANTUM) if value is not None else None


@dataclass
class ScaledLine:
    """One recipe line at a given scale."""

    line: RecipeIngredient
    ingredient: Ingredient | None  # the catalog entry, when the line is linked
    quantity: Decimal | None
    quantity_max: Decimal | None

    @property
    def amount(self) -> Decimal | None:
        """What to buy: the top of a range ("2-3 cloves" -> 3)."""
        return self.quantity_max if self.quantity_max is not None else self.quantity

    @property
    def name(self) -> str:
        return self.ingredient.name if self.ingredient is not None else self.line.raw_text


def scaled_lines(session: Session, recipe: Recipe, scale: Decimal) -> list[ScaledLine]:
    """The recipe's live ingredient lines, in order, scaled."""
    lines = session.scalars(
        select(RecipeIngredient)
        .where(RecipeIngredient.recipe_id == recipe.id, RecipeIngredient.deleted_at.is_(None))
        .order_by(RecipeIngredient.position, RecipeIngredient.id)
    ).all()
    return [
        ScaledLine(
            line=line,
            ingredient=session.get(Ingredient, line.ingredient_id) if line.ingredient_id else None,
            quantity=_scaled(line.quantity, scale),
            quantity_max=_scaled(line.quantity_max, scale),
        )
        for line in lines
    ]


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
    items = []
    for scaled in scaled_lines(svc.session, recipe, scale):
        line, ingredient = scaled.line, scaled.ingredient
        if ingredient is not None:
            result = svc.add_item(
                list_id,
                ingredient.name,
                scaled.amount,
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
            scale_factor(recipe, entry.servings),
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
        svc, list_id, recipe, scale_factor(recipe, servings), ItemSourceType.RECIPE, None
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


def remove_entry_from_lists(svc: ShoppingService, entry_id: uuid.UUID) -> int:
    """A plan entry is going away: subtract its share from every list it was added to."""
    sources = list(
        svc.session.scalars(
            select(ShoppingItemSource).where(
                ShoppingItemSource.household_id == svc.household_id,
                ShoppingItemSource.meal_plan_entry_id == entry_id,
                ShoppingItemSource.deleted_at.is_(None),
            )
        )
    )
    _remove_sources(svc, sources)
    return len(sources)


def delete_entry(svc: ShoppingService, entry: MealPlanEntry) -> None:
    """Soft-delete a plan entry and take its share off the shopping lists (REST and sync)."""
    if entry.deleted_at is not None:
        return
    remove_entry_from_lists(svc, entry.id)
    entry.deleted_at = utcnow()


# ------------------------------------------------------------------ plan expansion
SLOT_ORDER = {slot: i for i, slot in enumerate(MealSlot)}


@dataclass
class LineSource:
    """One recipe line's share of a merged line."""

    entry: MealPlanEntry
    recipe: Recipe
    scaled: ScaledLine


@dataclass
class PlanLine:
    ingredient_id: uuid.UUID | None
    name: str
    quantity: Decimal | None
    unit: str | None
    sources: list[LineSource] = field(default_factory=list)


@dataclass
class PlanExpansion:
    lines: list[PlanLine]
    # Entries that add no ingredients: free-text meals, or a recipe since deleted.
    other_entries: list[MealPlanEntry]


def plan_entries(
    session: Session, household_id: uuid.UUID, start: dt.date, end: dt.date
) -> list[MealPlanEntry]:
    """Live entries in [start, end], by date, then breakfast -> snack, then creation."""
    entries = session.scalars(
        select(MealPlanEntry).where(
            MealPlanEntry.household_id == household_id,
            MealPlanEntry.deleted_at.is_(None),
            MealPlanEntry.date >= start,
            MealPlanEntry.date <= end,
        )
    ).all()
    return sorted(
        entries, key=lambda e: (e.date, SLOT_ORDER.get(e.meal_slot, 99), e.created_at, e.id)
    )


def expand_plan(
    session: Session, household_id: uuid.UUID, start: dt.date, end: dt.date
) -> PlanExpansion:
    """The plan's ingredients for a date range, scaled to each entry's servings and merged.

    Lines linked to the same catalog ingredient merge when their units are compatible
    (cups + tbsp, g + lb) or one side has no amount, exactly as they would on the shopping
    list; incompatible units (cups vs g) stay separate lines. Lines not linked to the
    catalog are kept one per recipe line, as written. Every merged line keeps its sources.
    """
    lines: list[PlanLine] = []
    other: list[MealPlanEntry] = []
    for entry in plan_entries(session, household_id, start, end):
        recipe = session.get(Recipe, entry.recipe_id) if entry.recipe_id else None
        if recipe is None or recipe.deleted_at is not None:
            other.append(entry)
            continue
        for scaled in scaled_lines(session, recipe, scale_factor(recipe, entry.servings)):
            source = LineSource(entry=entry, recipe=recipe, scaled=scaled)
            unit = units.normalize(scaled.line.unit)
            target = None
            if scaled.ingredient is not None:
                target = next(
                    (
                        pl
                        for pl in lines
                        if pl.ingredient_id == scaled.ingredient.id
                        and units.mergeable(pl.quantity, pl.unit, scaled.amount, unit)
                    ),
                    None,
                )
            if target is None:
                ingredient_id = scaled.ingredient.id if scaled.ingredient else None
                lines.append(PlanLine(ingredient_id, scaled.name, scaled.amount, unit, [source]))
                continue
            target.quantity, target.unit = units.merge(
                target.quantity, target.unit, scaled.amount, unit
            )
            target.sources.append(source)
    return PlanExpansion(lines=lines, other_entries=other)
