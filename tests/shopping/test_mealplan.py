"""Meal plan -> list: scaled, merged, and un-merged when a recipe leaves the plan."""

import datetime as dt
from decimal import Decimal

import pytest

from thymble.models import Ingredient, MealPlanEntry, MealSlot, Recipe, RecipeIngredient
from thymble.shopping import mealplan

pytestmark = pytest.mark.db

MONDAY = dt.date(2026, 11, 2)
WEEK = dict(start=MONDAY, end=MONDAY + dt.timedelta(days=6))


def _recipe(s, hh, title, yield_qty, lines):
    recipe = Recipe(household_id=hh, title=title, yield_quantity=Decimal(yield_qty))
    s.add(recipe)
    s.flush()
    for pos, (raw, qty, unit, ing) in enumerate(lines):
        s.add(
            RecipeIngredient(
                household_id=hh,
                recipe_id=recipe.id,
                position=pos,
                raw_text=raw,
                quantity=Decimal(qty) if qty is not None else None,
                unit=unit,
                ingredient_id=ing.id if ing else None,
            )
        )
    s.flush()
    return recipe


def _entry(hh, day, recipe, servings=None):
    return MealPlanEntry(
        household_id=hh,
        date=MONDAY + dt.timedelta(days=day),
        meal_slot=MealSlot.DINNER,
        recipe_id=recipe.id,
        servings=Decimal(servings) if servings else None,
    )


@pytest.fixture
def plan(svc, setup, household):
    s = svc.session
    milk = Ingredient(name="milk")  # global catalog rows
    flour = Ingredient(name="flour")
    s.add_all([milk, flour])
    s.flush()
    pancakes = _recipe(
        s,
        household,
        "Pancakes",
        4,
        [
            ("1 cup milk", 1, "cup", milk),
            ("200 g flour", 200, "g", flour),
            ("a pinch of love", None, None, None),
        ],
    )
    sauce = _recipe(
        s,
        household,
        "Sauce",
        2,
        [("4 tbsp milk", 4, "tbsp", milk), ("1 lb flour", 1, "lb", flour)],
    )
    entries = (
        _entry(household, 0, pancakes, servings=8),
        _entry(household, 1, sauce),
        _entry(household, 9, sauce),
    )
    s.add_all(entries)
    s.flush()
    return {"milk": milk, "flour": flour, "entries": entries, "sauce": sauce}


def _by_ingredient(svc, setup):
    return {i.ingredient_id: i for i in svc.items(setup.list.id)}


def test_week_is_scaled_and_merged(svc, setup, plan):
    mealplan.add_meal_plan(svc, setup.list.id, **WEEK)
    items = _by_ingredient(svc, setup)
    milk, flour = items[plan["milk"].id], items[plan["flour"].id]
    # Pancakes doubled (8 servings / yield 4): 2 cup milk + 4 tbsp = 2.25 cup.
    assert (milk.quantity, milk.unit) == (Decimal("2.2500"), "cup")
    # 400 g + 1 lb (453.592 g)
    assert (flour.quantity, flour.unit) == (Decimal("853.5920"), "g")
    assert items[None].display_text == "a pinch of love"
    assert len(items) == 3  # the third entry is outside the week


def test_adding_again_is_idempotent(svc, setup, plan):
    for _ in range(2):
        mealplan.add_meal_plan(svc, setup.list.id, **WEEK)
    assert _by_ingredient(svc, setup)[plan["milk"].id].quantity == Decimal("2.2500")


def test_removing_an_entry_subtracts_its_share(svc, setup, plan):
    e1, e2, _ = plan["entries"]
    mealplan.add_meal_plan(svc, setup.list.id, entry_ids=[e1.id, e2.id])
    mealplan.remove_meal_plan_entry(svc, setup.list.id, e2.id)
    items = _by_ingredient(svc, setup)
    assert items[plan["milk"].id].quantity == Decimal("2.0000")
    assert items[plan["flour"].id].quantity == Decimal("400.0000")
    mealplan.remove_meal_plan_entry(svc, setup.list.id, e1.id)
    assert svc.items(setup.list.id) == []


def test_manual_add_survives_recipe_removal(svc, setup, plan):
    e1 = plan["entries"][0]
    manual = svc.add_item(setup.list.id, "milk").item  # voice/app "milk", no quantity
    mealplan.add_meal_plan(svc, setup.list.id, entry_ids=[e1.id])
    assert manual.quantity == Decimal("2.0000")
    mealplan.remove_meal_plan_entry(svc, setup.list.id, e1.id)
    assert manual.deleted_at is None and manual.quantity is None


def test_bought_items_keep_their_share(svc, setup, plan):
    e1 = plan["entries"][0]
    mealplan.add_meal_plan(svc, setup.list.id, entry_ids=[e1.id])
    milk = _by_ingredient(svc, setup)[plan["milk"].id]
    svc.mark_bought(milk)
    mealplan.remove_meal_plan_entry(svc, setup.list.id, e1.id)
    assert milk.deleted_at is None and milk.status.value == "bought"


def test_add_recipe_with_servings_and_remove(svc, setup, plan):
    mealplan.add_recipe(svc, setup.list.id, plan["sauce"].id, Decimal(4))
    assert _by_ingredient(svc, setup)[plan["milk"].id].quantity == Decimal("8.0000")
    mealplan.remove_recipe(svc, setup.list.id, plan["sauce"].id)
    assert svc.items(setup.list.id) == []
