"""REST endpoints for lists, items, stores, groups, prefs, trips and connector accounts."""

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_settings
from thymble.api.deps import CurrentHousehold, get_current_household, get_db
from thymble.connectors.crypto import decrypt_credentials
from thymble.main import create_app
from thymble.models import ConnectorAccount, Household

pytestmark = pytest.mark.db

SETTINGS = dict(connectors_lists=["home_assistant"], connector_encryption_key="test-key-123")


@pytest.fixture
def api(sessions, household):
    app = create_app(make_settings(**SETTINGS))

    def db():
        with sessions() as s:
            yield s

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_household] = lambda: CurrentHousehold(household)
    return TestClient(app)


def ok(response, code=200):
    assert response.status_code == code, response.text
    return response.json() if response.content else None


def test_unauthenticated_requests_are_rejected(sessions):
    client = TestClient(create_app(make_settings()))
    assert client.get("/v1/shopping/lists").status_code == 401
    # The dev-only header stand-in is off outside development.
    hdr = {"X-Thymble-Household": str(uuid.uuid4())}
    assert client.get("/v1/shopping/lists", headers=hdr).status_code == 401


def test_list_item_flow(api):
    lst = ok(api.post("/v1/shopping/lists", json={"name": "Groceries"}), 201)
    costco = ok(api.post("/v1/stores", json={"name": "Costco", "is_bulk": True}), 201)
    aldi = ok(api.post("/v1/stores", json={"name": "Aldi"}), 201)
    group = ok(
        api.post(
            "/v1/store-groups",
            json={"name": "Everyday", "is_default": True, "store_ids": [aldi["id"]]},
        ),
        201,
    )
    assert group["store_ids"] == [aldi["id"]]

    items_url = f"/v1/shopping/lists/{lst['id']}/items"
    tp = ok(api.post(items_url, json={"name": "toilet paper", "store_id": costco["id"]}), 201)
    assert tp["item"]["store_id"] == costco["id"]
    urgent = ok(api.post(items_url, json={"text": "urgent toilet paper"}), 201)
    assert urgent["merged"] and urgent["item"]["urgency"] == "urgent"
    assert urgent["stop_gap"]["store_group_id"] == group["id"]

    eggs = ok(api.post(items_url, json={"text": "2 dozen eggs"}), 201)["item"]
    patch = {"display_text": "eggs", "aisle": "dairy"}
    eggs = ok(api.patch(f"/v1/shopping/items/{eggs['id']}", json=patch))["item"]
    assert eggs["aisle"] == "dairy"
    ok(api.post(f"/v1/shopping/items/{eggs['id']}/check", json={"store_id": aldi["id"]}))
    assert ok(api.get(f"/v1/shopping/items/{eggs['id']}"))["status"] == "bought"
    ok(api.post(f"/v1/shopping/items/{eggs['id']}/uncheck"))
    ok(api.delete(f"/v1/shopping/items/{eggs['id']}"), 204)
    assert api.get(f"/v1/shopping/items/{eggs['id']}").status_code == 404

    needed = ok(api.get(items_url, params={"status": "needed"}))
    assert sorted(i["display_text"] for i in needed) == ["toilet paper", "toilet paper"]

    prefs = ok(api.get("/v1/item-prefs", params={"ingredient_id": tp["item"]["ingredient_id"]}))
    assert [(p["store_id"], p["is_home"]) for p in prefs] == [(costco["id"], True)]
    pref_in = {
        "ingredient_id": tp["item"]["ingredient_id"],
        "store_id": aldi["id"],
        "preferred_brand": "Charmin",
        "typical_quantity": "4",
        "typical_unit": "each",
    }
    pref = ok(api.put("/v1/item-prefs", json=pref_in))
    assert pref["preferred_brand"] == "Charmin" and pref["typical_unit"] == "ct"
    home = ok(api.put(f"/v1/item-prefs/{tp['item']['ingredient_id']}/home",
                      json={"store_id": aldi["id"]}))  # fmt: skip
    assert home["is_home"] and home["id"] == pref["id"]


def test_errors_are_mapped(api):
    lst = ok(api.post("/v1/shopping/lists", json={"name": "L"}), 201)
    r = api.post(f"/v1/shopping/lists/{lst['id']}/items", json={})
    assert r.status_code == 400
    assert api.get(f"/v1/shopping/items/{uuid.uuid4()}").status_code == 404


def test_other_households_data_is_invisible(api, sessions):
    with sessions() as s:
        other = Household(name="Other")
        s.add(other)
        s.commit()
        other_id = other.id
    lst = ok(api.post("/v1/shopping/lists", json={"name": "Mine"}), 201)
    app = api.app
    app.dependency_overrides[get_current_household] = lambda: CurrentHousehold(other_id)
    assert api.get(f"/v1/shopping/lists/{lst['id']}/items").status_code == 404
    assert ok(api.get("/v1/shopping/lists")) == []


def test_trip_endpoints(api):
    lst = ok(api.post("/v1/shopping/lists", json={"name": "L"}), 201)
    walmart = ok(api.post("/v1/stores", json={"name": "Walmart", "supports_pickup": True}), 201)
    items_url = f"/v1/shopping/lists/{lst['id']}/items"
    milk = ok(api.post(items_url, json={"name": "milk", "store_id": walmart["id"]}), 201)["item"]
    soap = ok(api.post(items_url, json={"name": "soap", "store_id": walmart["id"]}), 201)["item"]

    trip = ok(api.post("/v1/trips", json={"store_id": walmart["id"], "kind": "pickup"}), 201)
    suggested = ok(api.get(f"/v1/trips/{trip['id']}/suggested"))
    assert {i["id"] for i in suggested} == {milk["id"], soap["id"]}
    export = ok(api.post(f"/v1/trips/{trip['id']}/export",
                         json={"item_ids": [milk["id"], soap["id"]]}))  # fmt: skip
    assert export["trip"]["status"] == "on_order" and len(export["lines"]) == 2
    view = ok(api.get(f"/v1/trips/{trip['id']}"))
    assert len(view["order_items"]) == 2 and view["get_here"] == []
    done = ok(api.post(f"/v1/trips/{trip['id']}/confirm", json={"missing_item_ids": [soap["id"]]}))
    assert done["status"] == "done"
    assert ok(api.get(f"/v1/shopping/items/{soap['id']}"))["status"] == "needed"
    assert ok(api.get(f"/v1/shopping/items/{milk['id']}"))["status"] == "bought"
    assert [t["id"] for t in ok(api.get("/v1/trips", params={"status": "done"}))] == [trip["id"]]

    instore = ok(api.post("/v1/trips", json={"store_id": walmart["id"], "kind": "in_store"}), 201)
    view = ok(api.get(f"/v1/trips/{instore['id']}"))
    assert [i["id"] for i in view["get_here"]] == [soap["id"]]
    ok(api.post(f"/v1/trips/{instore['id']}/items/{soap['id']}/check"))
    assert ok(api.post(f"/v1/trips/{instore['id']}/finish"))["status"] == "done"


def test_connector_accounts_never_return_credentials(api, sessions):
    lst = ok(api.post("/v1/shopping/lists", json={"name": "L"}), 201)
    body = {
        "kind": "home_assistant",
        "display_name": "Home",
        "base_url": "http://homeassistant.local:8123",
        "token": "secret-long-lived-token",
        "config": {"main_entity": "todo.shopping_list", "list_id": lst["id"], "dry_run": True},
    }
    created = ok(api.post("/v1/connectors", json=body), 201)
    assert "token" not in created and created["has_credentials"]
    assert "secret-long-lived-token" not in api.get("/v1/connectors").text
    with sessions() as s:
        account = s.get(ConnectorAccount, uuid.UUID(created["id"]))
        assert b"secret-long-lived-token" not in account.credentials_encrypted
        creds = decrypt_credentials(make_settings(**SETTINGS), account.credentials_encrypted)
        assert creds == {"token": "secret-long-lived-token"}

    costco = ok(api.post("/v1/stores", json={
        "name": "Costco", "external_list_connector_account_id": created["id"],
        "external_list_id": "todo.costco"}), 201)  # fmt: skip
    assert costco["external_list_id"] == "todo.costco"

    bad = dict(body, config={})
    assert api.post("/v1/connectors", json=bad).status_code == 400  # main_entity missing
    assert api.post("/v1/connectors", json=dict(body, base_url="ftp://x")).status_code == 400
    assert api.post("/v1/connectors", json=dict(body, kind="walmart")).status_code == 400

    patched = ok(api.patch(f"/v1/connectors/{created['id']}", json={"status": "disabled"}))
    assert patched["status"] == "disabled"
    ok(api.delete(f"/v1/connectors/{created['id']}"), 204)
    assert ok(api.get("/v1/connectors")) == []
    assert ok(api.get("/v1/stores"))[0]["external_list_id"] is None


def test_connector_needs_encryption_key(sessions, household):
    app = create_app(make_settings(connectors_lists=["home_assistant"]))
    app.dependency_overrides[get_current_household] = lambda: CurrentHousehold(household)

    def db():
        with sessions() as s:
            yield s

    app.dependency_overrides[get_db] = db
    r = TestClient(app).post("/v1/connectors", json={
        "kind": "home_assistant", "base_url": "http://ha:8123", "token": "t",
        "config": {"main_entity": "todo.x"}})  # fmt: skip
    assert r.status_code == 503
