"""PowerSync endpoints: the sync token and the upload (client write) path."""

from __future__ import annotations

import uuid
from decimal import Decimal

import jwt
import pytest
from sqlalchemy import func, select

from tests.auth.conftest import SignedIn
from thymble.models import (
    Ingredient,
    MealPlanEntry,
    Recipe,
    RecipeStep,
    ShoppingItem,
    ShoppingItemSource,
    ShoppingList,
    Store,
    Trip,
)
from thymble.sync import SyncTable, register, registered_tables

pytestmark = pytest.mark.db


def test_sync_token_endpoint(api, alice):
    assert api.get("/v1/sync/token").status_code == 401
    response = api.get("/v1/sync/token", headers=alice.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["endpoint"] == "https://sync.thymble.test"
    claims = jwt.decode(body["token"], options={"verify_signature": False})
    assert claims["sub"] == alice.user_id
    assert claims["aud"] == "thymble-powersync"


def upload(api, user: SignedIn, *entries: dict):
    return api.post("/v1/sync/upload", json={"batch": list(entries)}, headers=user.headers)


def put(table: str, row_id: uuid.UUID, **data) -> dict:
    return {"op": "PUT", "table": table, "id": str(row_id), "data": data}


def patch(table: str, row_id: uuid.UUID, **data) -> dict:
    return {"op": "PATCH", "table": table, "id": str(row_id), "data": data}


def delete(table: str, row_id: uuid.UUID) -> dict:
    return {"op": "DELETE", "table": table, "id": str(row_id)}


def test_put_patch_delete_recipe(api, alice, db):
    rid = uuid.uuid4()
    created = upload(api, alice, put("recipe", rid, household_id=alice.household_id, title="Soup"))
    assert created.status_code == 200, created.text
    assert created.json() == {"applied": 1}
    recipe = db.get(Recipe, rid)
    assert recipe.title == "Soup"
    assert str(recipe.created_by) == alice.user_id

    assert upload(api, alice, patch("recipe", rid, title="Stew", rating=4)).status_code == 200
    db.expire_all()
    recipe = db.get(Recipe, rid)
    assert (recipe.title, recipe.rating) == ("Stew", 4)

    assert upload(api, alice, delete("recipe", rid)).status_code == 200
    db.expire_all()
    assert db.get(Recipe, rid).deleted_at is not None
    # Deleting again, or deleting something that never existed, is a no-op.
    assert (
        upload(api, alice, delete("recipe", rid), delete("recipe", uuid.uuid4())).status_code == 200
    )


def test_powersync_field_names_accepted(api, alice, db):
    rid = uuid.uuid4()
    entry = {
        "op": "PUT",
        "type": "recipe",
        "id": str(rid),
        "opData": {"household_id": alice.household_id, "title": "Bread"},
    }
    assert upload(api, alice, entry).status_code == 200
    assert db.get(Recipe, rid).title == "Bread"


def test_shopping_list_and_items_with_coercion(api, alice, db):
    list_id, item_id, step_id, rid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hid = alice.household_id
    response = upload(
        api,
        alice,
        put("shopping_list", list_id, household_id=hid, name="Groceries", is_active=1),
        put(
            "shopping_item",
            item_id,
            household_id=hid,
            list_id=str(list_id),
            display_text="2 onions",
            quantity=2.5,
            status="needed",
            urgency="urgent",
        ),
        patch("shopping_item", item_id, status="bought", checked_at="2026-10-29T12:00:00Z"),
        put("recipe", rid, household_id=hid, title="Pie"),
        put(
            "recipe_step",
            step_id,
            household_id=hid,
            recipe_id=str(rid),
            position=1,
            text="Bake",
            timers='[{"label": "bake", "seconds": 1500}]',
        ),
    )
    assert response.status_code == 200, response.text
    assert db.get(ShoppingList, list_id).is_active is True
    item = db.get(ShoppingItem, item_id)
    assert item.quantity == Decimal("2.5")
    assert item.status == "bought"
    assert str(item.checked_by) == alice.user_id
    assert db.get(RecipeStep, step_id).timers == [{"label": "bake", "seconds": 1500}]


def test_global_catalog_reference_allowed(api, alice, db):
    ingredient = Ingredient(id=uuid.uuid4(), name="onion", household_id=None)
    db.add(ingredient)
    db.commit()
    list_id, item_id = uuid.uuid4(), uuid.uuid4()
    hid = alice.household_id
    response = upload(
        api,
        alice,
        put("shopping_list", list_id, household_id=hid, name="L"),
        put(
            "shopping_item",
            item_id,
            household_id=hid,
            list_id=str(list_id),
            display_text="onion",
            ingredient_id=str(ingredient.id),
        ),
    )
    assert response.status_code == 200, response.text


def test_other_households_are_off_limits(api, alice, bob, db):
    # Bob can't create rows in Alice's household...
    rid = uuid.uuid4()
    response = upload(api, bob, put("recipe", rid, household_id=alice.household_id, title="x"))
    assert response.status_code == 403
    assert response.json()["detail"]["index"] == 0
    # ...or touch her rows.
    upload(api, alice, put("recipe", rid, household_id=alice.household_id, title="Mine"))
    for entry in (
        patch("recipe", rid, title="Hacked"),
        delete("recipe", rid),
        put("recipe", rid, household_id=bob.household_id, title="Hacked"),
    ):
        assert upload(api, bob, entry).status_code == 403
    db.expire_all()
    recipe = db.get(Recipe, rid)
    assert (recipe.title, recipe.deleted_at) == ("Mine", None)


def test_foreign_keys_must_stay_in_household(api, alice, bob, db):
    bobs_list = uuid.uuid4()
    upload(api, bob, put("shopping_list", bobs_list, household_id=bob.household_id, name="Bob"))
    response = upload(
        api,
        alice,
        put(
            "shopping_item",
            uuid.uuid4(),
            household_id=alice.household_id,
            list_id=str(bobs_list),
            display_text="sneaky",
        ),
    )
    assert response.status_code == 422
    assert "another household" in response.json()["detail"]["error"]


def test_batch_is_all_or_nothing(api, alice, db):
    good, bad = uuid.uuid4(), uuid.uuid4()
    response = upload(
        api,
        alice,
        put("recipe", good, household_id=alice.household_id, title="Fine"),
        put("recipe", bad, household_id=alice.household_id, title="Bad", rating=9),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["index"] == 1
    assert db.scalar(select(func.count()).select_from(Recipe)) == 0


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (lambda h: put("connector_account", uuid.uuid4(), household_id=h, kind="x"), "table"),
        (lambda h: put("household_member", uuid.uuid4(), household_id=h), "table"),
        (lambda h: put("recipe", uuid.uuid4(), household_id=h, title="x", secret=1), "secret"),
        (
            lambda h: put(
                "recipe", uuid.uuid4(), household_id=h, title="x", created_by=str(uuid.uuid4())
            ),
            "created_by",
        ),
        (
            lambda h: put(
                "recipe", uuid.uuid4(), household_id=h, title="x", deleted_at="2026-01-01T00:00:00Z"
            ),
            "deleted_at",
        ),
        (lambda h: put("recipe", uuid.uuid4(), title="x"), "household_id"),
        (lambda h: put("recipe", uuid.uuid4(), household_id=h, title=None), "title"),
        (
            lambda h: put(
                "recipe", uuid.uuid4(), household_id=h, title="x", source_type="carrier-pigeon"
            ),
            "source_type",
        ),
        (
            lambda h: put(
                "recipe", uuid.uuid4(), household_id=h, title="x", prep_time_minutes="soon"
            ),
            "prep_time_minutes",
        ),
        (lambda h: {"op": "PUT", "table": "recipe", "id": "not-a-uuid", "data": {}}, "UUID"),
        (lambda h: patch("recipe", uuid.uuid4(), title="ghost"), "exist"),
    ],
)
def test_invalid_entries_rejected(api, alice, entry, message):
    response = upload(api, alice, entry(alice.household_id))
    assert response.status_code == 422, response.text
    assert message in response.json()["detail"]["error"]


def test_rows_cannot_change_household(api, alice, db):
    other = api.post("/v1/households", json={"name": "Cabin"}, headers=alice.headers).json()
    rid = uuid.uuid4()
    upload(api, alice, put("recipe", rid, household_id=alice.household_id, title="x"))
    response = upload(api, alice, patch("recipe", rid, household_id=other["id"]))
    assert response.status_code == 422


def test_joined_member_can_write(api, alice, bob, db):
    code = api.post(f"/v1/households/{alice.household_id}/invites", headers=alice.headers).json()
    api.post("/v1/households/join", json={"code": code["code"]}, headers=bob.headers)
    rid = uuid.uuid4()
    response = upload(api, bob, put("recipe", rid, household_id=alice.household_id, title="Ours"))
    assert response.status_code == 200
    assert str(db.get(Recipe, rid).created_by) == bob.user_id


def test_requires_auth(api):
    assert api.post("/v1/sync/upload", json={"batch": []}).status_code == 401


def test_writable_tables_listing(api, alice):
    tables = {
        t["table"]: t["columns"] for t in api.get("/v1/sync/tables", headers=alice.headers).json()
    }
    assert {"recipe", "shopping_item", "shopping_list"} <= tables.keys()
    assert "created_by" not in tables["recipe"]
    assert "checked_by" not in tables["shopping_item"]
    assert "household_id" not in tables["recipe"]


def test_registry_rejects_unknown_columns():
    with pytest.raises(ValueError):
        register(SyncTable(Recipe, columns=frozenset({"nope"})))
    assert registered_tables()["recipe"].model is Recipe


SERVER_ONLY_TABLES = {
    "app_user",
    "auth_email_token",
    "auth_refresh_token",
    "household",
    "household_member",
    "household_invite",
    "import_job",
    "llm_usage",
    "llm_cache",
    "connector_account",
    "connector_item_link",
    "learning_event",
    "ingredient",
    "ingredient_alias",
}


def test_server_only_tables_are_not_writable():
    assert not SERVER_ONLY_TABLES & registered_tables().keys()


def test_meal_plan_stores_and_trips(api, alice, bob, db):
    hid = alice.household_id
    rid, entry_id, store_id, group_id, member_id, trip_id = (uuid.uuid4() for _ in range(6))
    list_id, item_id = uuid.uuid4(), uuid.uuid4()
    response = upload(
        api,
        alice,
        put("recipe", rid, household_id=hid, title="Chili"),
        put("shopping_list", list_id, household_id=hid, name="L"),
        put("shopping_item", item_id, household_id=hid, list_id=str(list_id), display_text="x"),
        put(
            "meal_plan_entry",
            entry_id,
            household_id=hid,
            date="2026-11-02",
            meal_slot="dinner",
            recipe_id=str(rid),
            servings=4,
        ),
        put("store", store_id, household_id=hid, name="Aldi", chain="aldi"),
        put("store_group", group_id, household_id=hid, name="Everyday", is_default=True),
        put(
            "store_group_member",
            member_id,
            household_id=hid,
            store_group_id=str(group_id),
            store_id=str(store_id),
        ),
        put("trip", trip_id, household_id=hid, store_id=str(store_id), kind="in_store"),
    )
    assert response.status_code == 200, response.text
    assert db.get(MealPlanEntry, entry_id).recipe_id == rid
    trip = db.get(Trip, trip_id)
    assert str(trip.user_id) == alice.user_id and trip.status == "planning"

    # Server-owned columns stay server-owned.
    for entry in (
        patch("trip", trip_id, status="done"),
        patch("store", store_id, external_list_id="todo.costco"),
        patch("shopping_item", item_id, trip_id=str(trip_id)),
        patch("shopping_item", item_id, bridge_of=str(item_id)),
    ):
        response = upload(api, alice, entry)
        assert response.status_code == 422, entry
        assert "not writable" in response.json()["detail"]["error"]
    assert db.get(Store, store_id).external_list_id is None

    # Bob can't plan meals with Alice's recipe.
    response = upload(
        api,
        bob,
        put(
            "meal_plan_entry",
            uuid.uuid4(),
            household_id=bob.household_id,
            date="2026-11-02",
            meal_slot="lunch",
            recipe_id=str(rid),
        ),
    )
    assert response.status_code == 422
    assert "another household" in response.json()["detail"]["error"]


def test_item_sources(api, alice, db):
    hid = alice.household_id
    list_id, item_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    batch = [
        put("shopping_list", list_id, household_id=hid, name="L"),
        put("shopping_item", item_id, household_id=hid, list_id=str(list_id), display_text="x"),
    ]
    response = upload(
        api,
        alice,
        *batch,
        put(
            "shopping_item_source",
            source_id,
            household_id=hid,
            shopping_item_id=str(item_id),
            source_type="manual",
        ),
    )
    assert response.status_code == 200, response.text
    assert db.get(ShoppingItemSource, source_id).source_type == "manual"
    response = upload(
        api,
        alice,
        put(
            "shopping_item_source",
            uuid.uuid4(),
            household_id=hid,
            shopping_item_id=str(item_id),
            source_type="connector",
        ),
    )
    assert response.status_code == 422
    assert "server" in response.json()["detail"]["error"]
