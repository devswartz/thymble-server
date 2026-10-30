"""Meal plan entries over REST, the plan -> ingredients expansion, and deletes taking a
plan entry's share off the shopping list (REST and sync alike)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from tests.shopping.test_api import ok
from thymble.models import Ingredient, MealPlanEntry, Recipe, ShoppingItem
from thymble.sync import registered_tables

pytestmark = pytest.mark.db

MONDAY = dt.date(2026, 11, 2)


def day(n: int) -> str:
    return (MONDAY + dt.timedelta(days=n)).isoformat()


WEEK = {"start": day(0), "end": day(6)}


@pytest.fixture
def catalog(sessions):
    """Global catalog rows the synthetic recipes link to."""
    with sessions() as s:
        rows = {name: Ingredient(name=name) for name in ("milk", "flour", "egg", "sugar", "garlic")}
        s.add_all(rows.values())
        s.commit()
        return {name: str(row.id) for name, row in rows.items()}


def line(raw, qty=None, unit=None, ing=None, qty_max=None) -> dict:
    return {
        "raw_text": raw,
        "quantity": qty,
        "quantity_max": qty_max,
        "unit": unit,
        "ingredient_id": ing,
    }


@pytest.fixture
def recipes(api, catalog):
    pancakes = ok(
        api.post(
            "/v1/recipes",
            json={
                "title": "Test pancakes",
                "yield_quantity": 4,
                "ingredients": [
                    line("1 cup milk", 1, "cup", catalog["milk"]),
                    line("2 cups flour", 2, "cups", catalog["flour"]),
                    line("2 eggs", 2, None, catalog["egg"]),
                    line("1 tbsp sugar", 1, "tbsp", catalog["sugar"]),
                ],
            },
        ),
        201,
    )
    bread = ok(
        api.post(
            "/v1/recipes",
            json={
                "title": "Test loaf",
                "yield_quantity": 2,
                "ingredients": [
                    line("250 g flour", 250, "g", catalog["flour"]),
                    line("1/2 cup milk", "0.5", "cup", catalog["milk"]),
                    line("2 tsp sugar", 2, "tsp", catalog["sugar"]),
                    line("2-3 cloves garlic", 2, "clove", catalog["garlic"], qty_max=3),
                    line("a handful of fresh herbs"),
                ],
            },
        ),
        201,
    )
    return {"pancakes": pancakes["id"], "bread": bread["id"]}


def add_entry(api, **body) -> dict:
    return ok(api.post("/v1/meal-plan", json=body), 201)


# ------------------------------------------------------------------------ entries
def test_entry_crud(api, recipes):
    dinner = add_entry(
        api, date=day(1), meal_slot="dinner", recipe_id=recipes["pancakes"], servings=8
    )
    assert dinner["recipe_title"] == "Test pancakes"
    assert Decimal(dinner["servings"]) == 8
    breakfast = add_entry(api, date=day(1), meal_slot="breakfast", recipe_id=recipes["bread"])
    out = add_entry(api, date=day(0), meal_slot="lunch", free_text="  eat   out ")
    assert out["free_text"] == "eat out" and out["recipe_id"] is None
    add_entry(api, date=day(9), meal_slot="dinner", free_text="next week")

    listed = ok(api.get("/v1/meal-plan", params=WEEK))
    # By date, then breakfast -> lunch -> dinner -> snack.
    assert [e["id"] for e in listed] == [out["id"], breakfast["id"], dinner["id"]]
    assert ok(api.get(f"/v1/meal-plan/{dinner['id']}")) == dinner

    moved = ok(
        api.patch(
            f"/v1/meal-plan/{dinner['id']}",
            json={"date": day(3), "meal_slot": "snack", "servings": None, "notes": "double"},
        )
    )
    assert (moved["date"], moved["meal_slot"], moved["servings"], moved["notes"]) == (
        day(3),
        "snack",
        None,
        "double",
    )
    assert moved["recipe_id"] == recipes["pancakes"]  # not sent, unchanged
    as_text = ok(
        api.patch(
            f"/v1/meal-plan/{dinner['id']}", json={"recipe_id": None, "free_text": "leftovers"}
        )
    )
    assert (as_text["recipe_id"], as_text["recipe_title"]) == (None, None)

    assert api.delete(f"/v1/meal-plan/{dinner['id']}").status_code == 204
    assert api.get(f"/v1/meal-plan/{dinner['id']}").status_code == 404
    assert api.delete(f"/v1/meal-plan/{dinner['id']}").status_code == 404
    assert [e["id"] for e in ok(api.get("/v1/meal-plan", params=WEEK))] == [
        out["id"],
        breakfast["id"],
    ]


def test_entry_validation(api, stranger_api, recipes):
    def post(**body):
        return api.post("/v1/meal-plan", json={"date": day(0), "meal_slot": "dinner", **body})

    assert post().status_code == 422  # neither recipe nor text
    assert post(free_text="   ").status_code == 422
    assert post(free_text="x", meal_slot="brunch").status_code == 422
    assert post(recipe_id=recipes["pancakes"], servings=0).status_code == 422
    assert post(recipe_id=str(uuid.uuid4())).status_code == 422
    theirs = ok(stranger_api.post("/v1/recipes", json={"title": "Their soup"}), 201)
    assert post(recipe_id=theirs["id"]).status_code == 422

    entry = add_entry(api, date=day(0), meal_slot="dinner", recipe_id=recipes["pancakes"])
    url = f"/v1/meal-plan/{entry['id']}"
    assert api.patch(url, json={"recipe_id": None}).status_code == 422  # nothing left
    assert api.patch(url, json={"date": None}).status_code == 422
    assert api.patch(url, json={"recipe_id": theirs["id"]}).status_code == 422
    assert ok(api.get(url))["recipe_id"] == recipes["pancakes"]

    # Client-generated ids work, once.
    own_id = str(uuid.uuid4())
    assert add_entry(api, id=own_id, date=day(0), meal_slot="snack", free_text="fruit")["id"] == (
        own_id
    )
    assert post(id=own_id, free_text="again").status_code == 409

    # Ranges.
    assert api.get("/v1/meal-plan", params={"start": day(3), "end": day(1)}).status_code == 422
    too_long = {"start": day(0), "end": (MONDAY + dt.timedelta(days=400)).isoformat()}
    assert api.get("/v1/meal-plan", params=too_long).status_code == 422
    assert api.get("/v1/meal-plan/ingredients", params=too_long).status_code == 422

    # Households don't see each other's plans.
    assert stranger_api.get(url).status_code == 404
    assert stranger_api.patch(url, json={"notes": "x"}).status_code == 404
    assert stranger_api.delete(url).status_code == 404
    assert ok(stranger_api.get("/v1/meal-plan", params=WEEK)) == []


# ---------------------------------------------------------------------- expansion
def by_name(expansion: dict) -> dict[str, list[dict]]:
    lines: dict[str, list[dict]] = {}
    for pl in expansion["lines"]:
        lines.setdefault(pl["name"], []).append(pl)
    return lines


def test_expansion_scales_merges_and_keeps_sources(api, sessions, recipes):
    # Pancakes for 8 (x2) on Monday and as written (x1) on Tuesday; the loaf for 4 (x2).
    mon = add_entry(api, date=day(0), meal_slot="dinner", recipe_id=recipes["pancakes"], servings=8)
    add_entry(api, date=day(1), meal_slot="breakfast", recipe_id=recipes["pancakes"])
    add_entry(api, date=day(2), meal_slot="lunch", recipe_id=recipes["bread"], servings=4)
    text = add_entry(api, date=day(3), meal_slot="dinner", free_text="leftovers")
    add_entry(api, date=day(8), meal_slot="dinner", recipe_id=recipes["pancakes"])  # outside
    gone = add_entry(api, date=day(5), meal_slot="lunch", recipe_id=recipes["bread"])
    api.delete(f"/v1/meal-plan/{gone['id']}")

    result = ok(api.get("/v1/meal-plan/ingredients", params=WEEK))
    assert (result["start"], result["end"]) == (WEEK["start"], WEEK["end"])
    lines = by_name(result)

    # milk: 2 cup + 1 cup + 2 x 0.5 cup
    [milk] = lines["milk"]
    assert (Decimal(milk["quantity"]), milk["unit"], milk["text"]) == (
        Decimal(4),
        "cup",
        "4 cup milk",
    )
    assert [s["recipe_title"] for s in milk["sources"]] == [
        "Test pancakes",
        "Test pancakes",
        "Test loaf",
    ]
    first = milk["sources"][0]
    assert first["meal_plan_entry_id"] == mon["id"]
    assert (first["date"], first["meal_slot"], Decimal(first["servings"])) == (day(0), "dinner", 8)
    assert (Decimal(first["quantity"]), first["unit"], first["raw_text"]) == (
        Decimal(2),
        "cup",
        "1 cup milk",
    )

    # flour: cups merge (4 + 2); grams can't be added to cups, so they stay separate.
    cups, grams = lines["flour"]
    assert (Decimal(cups["quantity"]), cups["unit"], len(cups["sources"])) == (Decimal(6), "cup", 2)
    assert (Decimal(grams["quantity"]), grams["unit"], len(grams["sources"])) == (
        Decimal(500),
        "g",
        1,
    )

    # sugar: 2 tbsp + 1 tbsp + 4 tsp, in the first line's unit.
    [sugar] = lines["sugar"]
    assert sugar["unit"] == "tbsp"
    assert abs(Decimal(sugar["quantity"]) - Decimal("4.3333")) < Decimal("0.001")

    # eggs have no unit: 4 + 2.
    [eggs] = lines["egg"]
    assert (Decimal(eggs["quantity"]), eggs["unit"], eggs["text"]) == (Decimal(6), None, "6 egg")

    # A range buys the top: "2-3 cloves" x2 -> 6; the source keeps both ends.
    [garlic] = lines["garlic"]
    assert (Decimal(garlic["quantity"]), garlic["unit"]) == (Decimal(6), "clove")
    src = garlic["sources"][0]
    assert (Decimal(src["quantity"]), Decimal(src["quantity_max"])) == (Decimal(4), Decimal(6))

    # A line not linked to the catalog comes through as written.
    [herbs] = lines["a handful of fresh herbs"]
    assert (herbs["ingredient_id"], herbs["quantity"], herbs["text"]) == (
        None,
        None,
        "a handful of fresh herbs",
    )

    assert [e["id"] for e in result["other_entries"]] == [text["id"]]
    # Nothing was written to any list.
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(ShoppingItem)) == 0


def test_expansion_lists_entries_whose_recipe_was_deleted(api, recipes):
    entry = add_entry(api, date=day(0), meal_slot="dinner", recipe_id=recipes["bread"])
    assert api.delete(f"/v1/recipes/{recipes['bread']}").status_code == 204
    result = ok(api.get("/v1/meal-plan/ingredients", params=WEEK))
    assert result["lines"] == []
    [other] = result["other_entries"]
    assert (other["id"], other["recipe_id"], other["recipe_title"]) == (
        entry["id"],
        recipes["bread"],
        None,
    )


def test_expansion_matches_what_add_week_to_list_puts_on_the_list(api, recipes, setup):
    """Preview and list come from the same scaling and merge code."""
    add_entry(api, date=day(0), meal_slot="dinner", recipe_id=recipes["pancakes"], servings=8)
    add_entry(api, date=day(2), meal_slot="lunch", recipe_id=recipes["bread"], servings=3)
    preview = ok(api.get("/v1/meal-plan/ingredients", params=WEEK))
    items = ok(api.post(f"/v1/shopping/lists/{setup.list.id}/meal-plan", json=WEEK))

    def key(ingredient_id, quantity, unit):
        return (ingredient_id, Decimal(quantity) if quantity is not None else None, unit)

    expected = sorted(
        (key(pl["ingredient_id"], pl["quantity"], pl["unit"]) for pl in preview["lines"]),
        key=str,
    )
    actual = sorted(
        (
            key(i["ingredient_id"], i["quantity"], i["unit"])
            for i in items
            if i["ingredient_id"] is not None or i["display_text"] == "a handful of fresh herbs"
        ),
        key=str,
    )
    assert expected == actual


# ----------------------------------------------------- deletes leave the list too
def list_items(api, setup) -> dict[str, dict]:
    return {i["display_text"]: i for i in ok(api.get(f"/v1/shopping/lists/{setup.list.id}/items"))}


@pytest.mark.parametrize("path", ["rest", "sync"])
def test_deleting_an_entry_takes_its_share_off_the_list(path, api, recipes, setup, sessions):
    keep = add_entry(api, date=day(0), meal_slot="dinner", recipe_id=recipes["pancakes"])
    drop = add_entry(api, date=day(1), meal_slot="dinner", recipe_id=recipes["bread"])
    ok(api.post(f"/v1/shopping/lists/{setup.list.id}/meal-plan", json=WEEK))
    before = list_items(api, setup)
    assert Decimal(before["milk"]["quantity"]) == Decimal("1.5")
    assert "garlic" in before

    if path == "rest":
        assert api.delete(f"/v1/meal-plan/{drop['id']}").status_code == 204
    else:
        entry = {"op": "DELETE", "table": "meal_plan_entry", "id": drop["id"]}
        ok(api.post("/v1/sync/upload", json={"batch": [entry]}))

    after = list_items(api, setup)
    assert Decimal(after["milk"]["quantity"]) == Decimal(1)  # the loaf's half cup is gone
    assert "garlic" not in after and "a handful of fresh herbs" not in after
    assert after["flour"]["unit"] == "cup"
    with sessions() as s:
        assert s.get(MealPlanEntry, uuid.UUID(drop["id"])).deleted_at is not None
        assert s.get(MealPlanEntry, uuid.UUID(keep["id"])).deleted_at is None


def test_bought_items_stay_when_their_entry_is_deleted(api, recipes, setup):
    entry = add_entry(api, date=day(0), meal_slot="dinner", recipe_id=recipes["bread"])
    ok(api.post(f"/v1/shopping/lists/{setup.list.id}/meal-plan", json=WEEK))
    garlic = list_items(api, setup)["garlic"]
    ok(api.post(f"/v1/shopping/items/{garlic['id']}/check"))
    api.delete(f"/v1/meal-plan/{entry['id']}")
    assert list_items(api, setup)["garlic"]["status"] == "bought"


def test_meal_plan_syncs_both_ways(api, member, recipes, sessions):
    assert "meal_plan_entry" in registered_tables()
    entry_id = uuid.uuid4()
    put = {
        "op": "PUT",
        "table": "meal_plan_entry",
        "id": str(entry_id),
        "data": {
            "household_id": str(member.household_id),
            "date": day(4),
            "meal_slot": "dinner",
            "recipe_id": recipes["pancakes"],
            "servings": 2,
        },
    }
    ok(api.post("/v1/sync/upload", json={"batch": [put]}))
    # A device's entry shows up over REST and in the expansion.
    assert ok(api.get(f"/v1/meal-plan/{entry_id}"))["recipe_title"] == "Test pancakes"
    lines = by_name(ok(api.get("/v1/meal-plan/ingredients", params=WEEK)))
    assert Decimal(lines["milk"][0]["quantity"]) == Decimal("0.5")
    with sessions() as s:
        assert s.get(Recipe, uuid.UUID(recipes["pancakes"])).deleted_at is None
