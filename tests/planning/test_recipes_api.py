"""REST recipes: list/search, read, create, edit, soft delete, scaled preview."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.shopping.test_api import ok
from thymble.models import Ingredient, Recipe, RecipeIngredient, RecipeStep, RecipeTag, Tag

pytestmark = pytest.mark.db


def make_recipe(api, title="Weeknight lentil soup", **extra) -> dict:
    body = {
        "title": title,
        "yield_quantity": 4,
        "yield_text": "Serves 4",
        "tags": ["Soup", "vegetarian"],
        "ingredients": [
            {"raw_text": "1 cup red lentils", "quantity": 1, "unit": "cups"},
            {"raw_text": "2 tablespoons olive oil", "quantity": 2, "unit": "Tablespoons"},
            {"raw_text": "1 pint vegetable stock", "quantity": 1, "unit": "pints"},
            {"raw_text": "2-3 cloves garlic", "quantity": 2, "quantity_max": 3, "unit": "cloves"},
            {"raw_text": "salt to taste"},
        ],
        "steps": [
            {"text": "Soften the garlic in the oil."},
            {"text": "Add lentils and stock; simmer 20 minutes.", "timers": [{"seconds": 1200}]},
        ],
    }
    body.update(extra)
    return ok(api.post("/v1/recipes", json=body), 201)


def test_create_and_get(api, member):
    recipe = make_recipe(api)
    assert recipe["source_type"] == "manual" and recipe["is_draft"] is False
    assert recipe["created_by"] == str(member.user_id)
    assert recipe["tags"] == ["Soup", "vegetarian"]
    lines = recipe["ingredients"]
    assert [line["position"] for line in lines] == [0, 1, 2, 3, 4]
    # Units are stored as the shared canonical codes.
    assert [line["unit"] for line in lines] == ["cup", "tbsp", "pt", "clove", None]
    assert recipe["steps"][0]["text"] == "Soften the garlic in the oil."
    assert recipe["steps"][1]["timers"] == [{"seconds": 1200}]

    got = ok(api.get(f"/v1/recipes/{recipe['id']}"))
    assert got["ingredients"] == lines
    assert got["scale"] == "1" and got["servings"] is None
    assert got["ingredients"][3]["scaled_quantity_max"] == "3.0000"


def test_create_validates(api, stranger_api, sessions):
    assert api.post("/v1/recipes", json={"title": ""}).status_code == 422
    assert api.post("/v1/recipes", json={"title": "x", "rating": 6}).status_code == 422
    bad_range = {"title": "x", "ingredients": [{"raw_text": "a", "quantity": 3, "quantity_max": 2}]}
    assert api.post("/v1/recipes", json=bad_range).status_code == 422
    # An ingredient another household owns can't be linked; global catalog rows can.
    with sessions() as s:
        theirs = Ingredient(name="their spice", household_id=None)
        s.add(theirs)
        s.flush()
        stranger_recipe = make_recipe(stranger_api)
        private = Ingredient(
            name="private spice",
            household_id=s.get(Recipe, uuid.UUID(stranger_recipe["id"])).household_id,
        )
        s.add(private)
        s.commit()
        global_id, private_id = theirs.id, private.id
    line = {"raw_text": "a pinch", "ingredient_id": str(private_id)}
    response = api.post("/v1/recipes", json={"title": "x", "ingredients": [line]})
    assert response.status_code == 422
    line["ingredient_id"] = str(global_id)
    created = ok(api.post("/v1/recipes", json={"title": "x", "ingredients": [line]}), 201)
    assert created["ingredients"][0]["ingredient_name"] == "their spice"


def test_list_search_and_filters(api, stranger_api, sessions):
    soup = make_recipe(api)
    make_recipe(api, title="Quick tomato soup", tags=["soup", "Quick"])
    make_recipe(api, title="Oat pancakes", tags=["breakfast"])
    make_recipe(api, title="100% rye bread", tags=[])
    make_recipe(stranger_api, title="Their lentil soup")
    with sessions() as s:
        s.get(Recipe, uuid.UUID(soup["id"])).is_draft = True
        s.commit()

    def titles(**params):
        return [r["title"] for r in ok(api.get("/v1/recipes", params=params))]

    assert titles() == [
        "100% rye bread",
        "Oat pancakes",
        "Quick tomato soup",
        "Weeknight lentil soup",
    ]
    assert titles(q="SOUP") == ["Quick tomato soup", "Weeknight lentil soup"]
    assert titles(q="soup lentil") == ["Weeknight lentil soup"]  # every word
    assert titles(q="100%") == ["100% rye bread"]  # wildcards are literal
    assert titles(q="%") == ["100% rye bread"]
    assert titles(tag="SOUP") == ["Quick tomato soup", "Weeknight lentil soup"]
    assert titles(tag="soup", q="quick") == ["Quick tomato soup"]
    assert titles(tag="nope") == []
    assert titles(draft=True) == ["Weeknight lentil soup"]
    assert len(titles(draft=False)) == 3
    assert titles(limit=2, offset=1) == ["Oat pancakes", "Quick tomato soup"]
    listed = ok(api.get("/v1/recipes", params={"q": "quick"}))[0]
    assert listed["tags"] == ["Quick", "Soup"]  # an existing tag keeps its spelling
    assert "ingredients" not in listed
    # Tags are shared within the household: "soup" and "Soup" are one tag.
    with sessions() as s:
        names = s.scalars(select(Tag.name).where(Tag.household_id == listed_household(s, soup)))
        assert sorted(n.lower() for n in names) == ["breakfast", "quick", "soup", "vegetarian"]


def listed_household(s, recipe: dict) -> uuid.UUID:
    return s.get(Recipe, uuid.UUID(recipe["id"])).household_id


def test_scaled_preview(api):
    recipe = make_recipe(api)
    scaled = ok(api.get(f"/v1/recipes/{recipe['id']}", params={"servings": 6}))
    assert Decimal(scaled["scale"]) == Decimal("1.5")
    lines = scaled["ingredients"]
    assert Decimal(lines[0]["scaled_quantity"]) == Decimal("1.5")
    assert lines[0]["quantity"] == "1.0000"  # the recipe itself is unchanged
    assert Decimal(lines[3]["scaled_quantity"]) == Decimal(3)
    assert Decimal(lines[3]["scaled_quantity_max"]) == Decimal("4.5")
    assert lines[4]["scaled_quantity"] is None
    assert api.get(f"/v1/recipes/{recipe['id']}", params={"servings": 0}).status_code == 422

    no_yield = make_recipe(api, title="Toast", yield_quantity=None)
    same = ok(api.get(f"/v1/recipes/{no_yield['id']}", params={"servings": 6}))
    assert same["scale"] == "1"
    assert same["ingredients"][0]["scaled_quantity"] == same["ingredients"][0]["quantity"]


def test_update_fields_tags_lines_and_steps(api, sessions):
    recipe = make_recipe(api)
    rid = recipe["id"]
    lentils, oil, stock, garlic, salt = recipe["ingredients"]
    first_step = recipe["steps"][0]

    patch = {
        "title": "Lentil soup",
        "rating": 5,
        "notes": None,
        "tags": ["soup", "Weeknight"],
        "ingredients": [
            {**garlic, "quantity": 3, "quantity_max": None},  # moved to the top, edited
            {"id": lentils["id"], "raw_text": "1 cup red lentils", "quantity": 1, "unit": "cup"},
            {"raw_text": "1 lemon", "quantity": 1},  # new
        ],
        "steps": [{"id": first_step["id"], "text": "Soften the garlic."}],
    }
    updated = ok(api.patch(f"/v1/recipes/{rid}", json=patch))
    assert (updated["title"], updated["rating"], updated["notes"]) == ("Lentil soup", 5, None)
    assert updated["yield_quantity"] == "4.000"  # not sent, not changed
    assert updated["tags"] == ["Soup", "Weeknight"]
    lines = updated["ingredients"]
    assert [line["id"] for line in lines[:2]] == [garlic["id"], lentils["id"]]
    assert [line["raw_text"] for line in lines] == [
        "2-3 cloves garlic",
        "1 cup red lentils",
        "1 lemon",
    ]
    assert lines[0]["quantity"] == "3.0000" and lines[0]["quantity_max"] is None
    assert [s["id"] for s in updated["steps"]] == [first_step["id"]]

    with sessions() as s:
        removed = {uuid.UUID(x["id"]) for x in (oil, stock, salt)}
        for line_id in removed:
            assert s.get(RecipeIngredient, line_id).deleted_at is not None
        assert s.get(RecipeStep, uuid.UUID(recipe["steps"][1]["id"])).deleted_at is not None
        links = s.scalars(select(RecipeTag).where(RecipeTag.recipe_id == uuid.UUID(rid))).all()
        assert len([link for link in links if link.deleted_at is None]) == 2
        assert len([link for link in links if link.deleted_at is not None]) == 1  # vegetarian


def test_update_rejects_bad_input_without_partial_writes(api, stranger_api):
    recipe = make_recipe(api)
    rid = recipe["id"]
    other = make_recipe(api, title="Other")
    foreign_line = other["ingredients"][0]["id"]
    bad = {"title": "Changed", "ingredients": [{"id": foreign_line, "raw_text": "x"}]}
    assert api.patch(f"/v1/recipes/{rid}", json=bad).status_code == 422
    assert api.patch(f"/v1/recipes/{rid}", json={"title": None}).status_code == 422
    assert api.patch(f"/v1/recipes/{rid}", json={"is_draft": None}).status_code == 422
    unchanged = ok(api.get(f"/v1/recipes/{rid}"))
    assert unchanged["title"] == recipe["title"]
    assert unchanged["ingredients"] == recipe["ingredients"]
    # Another household's recipe is invisible.
    assert stranger_api.get(f"/v1/recipes/{rid}").status_code == 404
    assert stranger_api.patch(f"/v1/recipes/{rid}", json={"title": "Mine"}).status_code == 404
    assert stranger_api.delete(f"/v1/recipes/{rid}").status_code == 404


def test_drafts_are_finalized_by_clearing_is_draft(api, sessions):
    recipe = make_recipe(api)
    with sessions() as s:
        s.get(Recipe, uuid.UUID(recipe["id"])).is_draft = True
        s.commit()
    assert (
        ok(api.patch(f"/v1/recipes/{recipe['id']}", json={"is_draft": False}))["is_draft"] is False
    )


def test_soft_delete_takes_lines_steps_and_tag_links(api, sessions):
    recipe = make_recipe(api)
    rid = uuid.UUID(recipe["id"])
    assert api.delete(f"/v1/recipes/{rid}").status_code == 204
    assert api.get(f"/v1/recipes/{rid}").status_code == 404
    assert api.delete(f"/v1/recipes/{rid}").status_code == 404
    assert ok(api.get("/v1/recipes")) == []
    with sessions() as s:
        assert s.get(Recipe, rid).deleted_at is not None
        for model in (RecipeIngredient, RecipeStep, RecipeTag):
            rows = s.scalars(select(model).where(model.recipe_id == rid)).all()
            assert rows and all(r.deleted_at is not None for r in rows)
        # Tags themselves stay for the household's other recipes.
        assert all(t.deleted_at is None for t in s.scalars(select(Tag)))
