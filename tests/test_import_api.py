"""Import API + worker job against a real PostgreSQL (throwaway database, migrated to head).

The arq queue is replaced by running the job inline; LLM providers are fakes.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from tests.accounts import Member, authed_settings, create_member
from tests.llm_fakes import FakeProvider
from thymble.api.imports import get_enqueue
from thymble.config import normalize_db_url
from thymble.db.deps import get_db
from thymble.importer.jobs import import_recipe
from thymble.llm import DbStore, LLMClient
from thymble.main import create_app

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "import"


@pytest.fixture(scope="module")
def db_url() -> Iterator[str]:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        pytest.skip("DATABASE_URL not set")
    admin_url = make_url(normalize_db_url(raw))
    name = f"thymble_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logging"] = False
    command.upgrade(cfg, "head")
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def sm(db_url):
    engine = create_engine(db_url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def llm_reply(request):
    if request.schema_name == "t3_lineparse":
        return {
            "quantity": 6,
            "quantity_max": None,
            "unit": "cups",
            "name": "vegetable broth",
            "preparation": None,
            "note": None,
            "additional": [],
            "alternatives": ["water"],
            "optional": False,
        }
    if request.schema_name == "t4_normalizedname":
        name = request.prompt.removeprefix("Ingredient name: ").strip().lower()
        canonical = {"onion": "yellow onion", "garlic": "garlic"}.get(name, name)
        return {"canonical_name": canonical, "category": "produce", "is_food": True}
    if request.schema_name == "t2_transcription":
        return {
            "lines": ["Tea", "Ingredients", "1 tea bag", "1 cup water", "Method", "Steep 4 min."]
        }
    raise AssertionError(request.schema_name)


@pytest.fixture
def env(sm):
    provider = FakeProvider("anthropic", llm_reply)
    settings = authed_settings(anthropic_api_key="sk-test-placeholder")
    llm = LLMClient(settings, providers={"anthropic": provider}, store=DbStore(sm))
    app = create_app(settings)

    def db():
        with sm() as session:
            yield session

    async def enqueue(job_id: str) -> None:
        await import_recipe({"settings": settings, "sessionmaker": sm, "llm": llm}, job_id)

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_enqueue] = lambda: enqueue
    return TestClient(app), provider


@pytest.fixture
def member(sm) -> Member:
    return create_member(sm, authed_settings())


@pytest.fixture
def household(member) -> uuid.UUID:
    return member.household_id


def post_page(client: TestClient, member: Member, name: str = "jsonld_graph.html"):
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return client.post(
        "/v1/imports",
        json={"html": html, "final_url": "https://example.test/lentil-soup"},
        headers=member.headers,
    )


def test_device_html_import_saves_a_draft_recipe(env, member, household, sm):
    from thymble.models import (
        ImportJob,
        Ingredient,
        IngredientAlias,
        LearningEvent,
        LlmCache,
        LlmUsage,
        Recipe,
        RecipeIngredient,
        RecipeStep,
    )

    client, provider = env
    response = post_page(client, member)
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]

    status = client.get(f"/v1/imports/{job_id}", headers=member.headers).json()
    assert status["status"] == "done", status
    assert status["final_url"] == "https://example.test/lentil-soup"
    assert status["result"]["title"] == "Weeknight Lentil Soup"
    assert status["needs_review"] is False

    with sm() as session:
        recipe = session.get(Recipe, uuid.UUID(status["recipe_id"]))
        assert recipe.is_draft and recipe.household_id == household
        assert recipe.source_url == "https://example.test/lentil-soup"
        assert (recipe.yield_quantity, recipe.yield_unit) == (6, "bowls")
        assert recipe.total_time_minutes == 55
        lines = session.scalars(
            select(RecipeIngredient)
            .where(RecipeIngredient.recipe_id == recipe.id)
            .order_by(RecipeIngredient.position)
        ).all()
        assert lines[0].raw_text == "\u25a2 2 Tbsps extra virgin olive oil"
        assert (lines[0].quantity, lines[0].unit) == (2, "tbsp")
        olive_oil = session.get(Ingredient, lines[0].ingredient_id)
        assert olive_oil.name == "extra-virgin olive oil" and olive_oil.household_id is None
        assert all(line.ingredient_id for line in lines)
        steps = session.scalars(select(RecipeStep).where(RecipeStep.recipe_id == recipe.id)).all()
        assert {s.group_name for s in steps} == {"Start the soup", "Simmer"}
        assert any(s.timers for s in steps)

        # "onion" was mapped by the LLM (T4): household alias + learning event.
        alias = session.scalar(
            select(IngredientAlias).where(
                IngredientAlias.alias == "onion", IngredientAlias.household_id == household
            )
        )
        assert session.get(Ingredient, alias.ingredient_id).name == "yellow onion"
        events = session.scalars(
            select(LearningEvent).where(LearningEvent.household_id == household)
        ).all()
        assert {e.payload["alias"] for e in events} >= {"onion", "garlic"}
        assert all(e.event_type == "alias_learned" for e in events)

        usage = session.scalars(select(LlmUsage).where(LlmUsage.household_id == household)).all()
        assert {u.task for u in usage} == {"T3", "T4"}
        assert session.scalar(select(LlmCache).where(LlmCache.task == "T3")) is not None
        job = session.get(ImportJob, uuid.UUID(job_id))
        assert job.input["html"].startswith("<!doctype html>")

    # Second import: aliases come from the database, the T3 answer from llm_cache.
    calls_before = len(provider.requests)
    assert post_page(client, member).status_code == 202
    assert len(provider.requests) == calls_before


def test_imports_are_private_to_the_household(env, member, sm):
    client, _ = env
    job_id = post_page(client, member).json()["id"]
    url = f"/v1/imports/{job_id}"
    other = create_member(sm, authed_settings(), name="Other cook")
    # A member of another household can't see it, in their household or by naming ours.
    assert client.get(url, headers=other.headers).status_code == 404
    assert client.get(url, headers=other.headers_for(member.household_id)).status_code == 404
    # A second member of the same household can.
    housemate = create_member(sm, authed_settings(), household_id=member.household_id)
    assert client.get(url, headers=housemate.headers).status_code == 200
    # No token, a bad token, or no household named: refused.
    assert client.get(url).status_code == 401
    bad = {**member.headers, "Authorization": "Bearer not-a-token"}
    assert client.get(url, headers=bad).status_code == 401
    no_household = {"Authorization": member.headers["Authorization"]}
    assert client.get(url, headers=no_household).status_code == 400


def test_failed_import_reports_the_reason(sm, member):
    settings = authed_settings()
    app = create_app(settings)
    llm = LLMClient(settings, providers={}, store=DbStore(sm))

    def db():
        with sm() as session:
            yield session

    async def enqueue(job_id: str) -> None:
        await import_recipe({"settings": settings, "sessionmaker": sm, "llm": llm}, job_id)

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_enqueue] = lambda: enqueue
    client = TestClient(app)
    job_id = post_page(client, member, "no_markup.html").json()["id"]
    status = client.get(f"/v1/imports/{job_id}", headers=member.headers).json()
    assert status["status"] == "failed"
    assert "no recipe found" in status["error"] and status["recipe_id"] is None


def test_photo_import_multipart(env, member, sm):
    from thymble.models import ImportJob

    client, _ = env
    response = client.post(
        "/v1/imports",
        files={"photo": ("card.jpg", b"\xff\xd8fake-jpeg", "image/jpeg")},
        headers=member.headers,
    )
    assert response.status_code == 202, response.text
    status = client.get(f"/v1/imports/{response.json()['id']}", headers=member.headers).json()
    assert status["status"] == "done", status
    assert status["source_kind"] == "photo"
    assert status["result"]["transcription"][0] == "Tea"
    with sm() as session:
        assert session.get(ImportJob, uuid.UUID(status["id"])).photo is None  # not kept

    bad = client.post(
        "/v1/imports",
        files={"photo": ("doc.pdf", b"%PDF", "application/pdf")},
        headers=member.headers,
    )
    assert bad.status_code == 415


@pytest.mark.parametrize(
    "body",
    [
        {"text": "a", "url": "https://example.test"},
        {"html": "<html></html>"},
        {"url": "ftp://example.test/x"},
        {},
    ],
)
def test_request_validation(env, member, body):
    client, _ = env
    response = client.post("/v1/imports", json=body, headers=member.headers)
    assert response.status_code == 422


def test_imports_need_sign_in_in_every_environment():
    for env_name in ("production", "development"):
        client = TestClient(create_app(authed_settings(thymble_env=env_name)))
        assert client.post("/v1/imports", json={"text": "x"}).status_code == 401


def test_without_an_llm_every_line_is_still_linked(sm):
    """No LLM: unseen names become household ingredients, so the list gets clean names."""
    from thymble.models import Ingredient, Recipe, RecipeIngredient

    settings = authed_settings()
    member = create_member(sm, settings)
    app = create_app(settings)
    llm = LLMClient(settings, providers={}, store=DbStore(sm))

    def db():
        with sm() as session:
            yield session

    async def enqueue(job_id: str) -> None:
        await import_recipe({"settings": settings, "sessionmaker": sm, "llm": llm}, job_id)

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_enqueue] = lambda: enqueue
    client = TestClient(app)
    job_id = post_page(client, member).json()["id"]
    status = client.get(f"/v1/imports/{job_id}", headers=member.headers).json()
    assert status["status"] == "done", status
    with sm() as session:
        recipe = session.get(Recipe, uuid.UUID(status["recipe_id"]))
        lines = session.scalars(
            select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)
        ).all()
        assert lines and all(line.ingredient_id for line in lines)
        names = {session.get(Ingredient, line.ingredient_id).name for line in lines}
        assert "onion" in names  # as written; with an LLM it maps to "yellow onion"
