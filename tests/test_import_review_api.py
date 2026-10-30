"""Import review against a real PostgreSQL (D-034, D-035): confidence is persisted with the
draft and reported by the import API, finalizing is refused while "low" items are untouched,
and every confirmation or edit becomes a learning event. Recipe text is written for these
tests."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.accounts import Member, authed_settings, create_member
from tests.llm_fakes import FakeProvider
from tests.test_import_api import db_url, post_page, sm  # noqa: F401 - shared fixtures
from thymble.api.imports import get_enqueue
from thymble.db.deps import get_db
from thymble.importer.jobs import import_recipe
from thymble.llm import DbStore, LLMClient
from thymble.main import create_app
from thymble.models import Ingredient, LearningEvent, Recipe, RecipeIngredient, RecipeStep

pytestmark = pytest.mark.db

CARD = ["Honey Tea", "Ingredients", "1/8 tsp honey", "1 cup water", "Method", "Steep 4 minutes."]


def reply(request):
    if request.schema_name == "t2_transcription":
        return {"lines": CARD}
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
        return {"canonical_name": name, "category": "pantry", "is_food": True}
    raise AssertionError(request.schema_name)


@pytest.fixture
def client(sm):  # noqa: F811
    settings = authed_settings(anthropic_api_key="sk-test-placeholder")
    provider = FakeProvider("anthropic", reply)
    llm = LLMClient(settings, providers={"anthropic": provider}, store=DbStore(sm))
    app = create_app(settings)

    def db():
        with sm() as session:
            yield session

    async def enqueue(job_id: str) -> None:
        await import_recipe({"settings": settings, "sessionmaker": sm, "llm": llm}, job_id)

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_enqueue] = lambda: enqueue
    return TestClient(app)


@pytest.fixture
def member(sm) -> Member:  # noqa: F811
    return create_member(sm, authed_settings())


def import_photo(client: TestClient, member: Member, device: str | None, scores=None) -> dict:
    data = {}
    if device is not None:
        data["device_ocr_text"] = device
    if scores is not None:
        data["device_ocr_confidences"] = json.dumps(scores)
    response = client.post(
        "/v1/imports",
        files={"photo": ("card.jpg", b"\xff\xd8fake-jpeg", "image/jpeg")},
        data=data,
        headers=member.headers,
    )
    assert response.status_code == 202, response.text
    status = client.get(f"/v1/imports/{response.json()['id']}", headers=member.headers).json()
    assert status["status"] == "done", status
    return status


def misread_card(client, member) -> dict:
    """Photo import where the two readings disagree on a fraction (low)."""
    return import_photo(client, member, "\n".join(CARD).replace("1/8 tsp", "1/2 tsp"))


def finalize(client, member, status, *decisions):
    return client.post(
        f"/v1/imports/{status['id']}/finalize",
        json={"decisions": list(decisions)},
        headers=member.headers,
    )


def settle_rest(status: dict, skip: set | None = None) -> list[dict]:
    """ "accepted" for every check item and "confirmed" for every low one, except `skip`."""
    skip = skip or set()
    out = []
    result = status["result"]
    for target, entries in (("ingredient", result["ingredients"]), ("step", result["steps"])):
        for index, entry in enumerate(entries):
            if (target, index) in skip or entry["confidence"] == "high":
                continue
            action = "confirmed" if entry["confidence"] == "low" else "accepted"
            out.append({"target": target, "index": index, "action": action})
    for section in result["sections"]:
        if ("section", section["field"]) in skip or section["confidence"] == "high":
            continue
        action = "confirmed" if section["confidence"] == "low" else "accepted"
        out.append({"target": "section", "field": section["field"], "action": action})
    return out


# ------------------------------------------------------------------ result wire format


def test_photo_import_reports_and_persists_confidence(client, member, sm):  # noqa: F811
    from thymble.models import ImportJob

    status = misread_card(client, member)
    assert status["needs_review"] is True
    assert status["review"]["state"] == "needs_review"
    assert status["review"]["counts"]["low"] == 1
    honey = status["result"]["ingredients"][0]
    assert honey["confidence"] == "low"
    assert isinstance(honey["parse_score"], float)
    assert "readings_number" in honey["reason_codes"]
    assert "the two readings of the photo differ in a number: 1/8 vs 1/2" in honey["reasons"]
    assert honey["device_reading"] == "1/2 tsp honey"
    assert honey["readings"]["model"] == "1/8 tsp honey"
    assert status["result"]["readings"]["device"][2] == "1/2 tsp honey"
    sections = status["result"]["sections"]
    assert [s["field"] for s in sections] == ["title", "yield", "times", "groups"]
    assert sections[0]["value"] == "Honey Tea" and sections[0]["confidence"] == "high"
    assert all({"field", "value", "confidence", "reasons"} <= s.keys() for s in sections)
    assert status["result"]["steps"][0]["confidence"] == "high"

    with sm() as session:
        recipe = session.get(Recipe, uuid.UUID(status["recipe_id"]))
        assert recipe.review_state == "needs_review" and recipe.is_draft
        assert recipe.review["source_kind"] == "photo"
        assert recipe.created_by == member.user_id
        line = session.scalars(
            select(RecipeIngredient)
            .where(RecipeIngredient.recipe_id == recipe.id)
            .order_by(RecipeIngredient.position)
        ).first()
        assert line.confidence == "low"
        assert line.review["readings"]["device"] == "1/2 tsp honey"
        step = session.scalars(select(RecipeStep).where(RecipeStep.recipe_id == recipe.id)).one()
        assert step.confidence == "high"
        job = session.get(ImportJob, uuid.UUID(status["id"]))
        assert job.input["device_ocr_text"].startswith("Honey Tea")


def test_device_ocr_fields_are_validated(client, member):
    def post(data):
        return client.post(
            "/v1/imports",
            files={"photo": ("card.jpg", b"\xff\xd8x", "image/jpeg")},
            data=data,
            headers=member.headers,
        )

    assert post({"device_ocr_text": "a\nb", "device_ocr_confidences": "[0.5]"}).status_code == 422
    assert post({"device_ocr_text": "a", "device_ocr_confidences": "nope"}).status_code == 422
    assert post({"device_ocr_text": "a", "device_ocr_confidences": "[2]"}).status_code == 422
    assert post({"device_ocr_confidences": "[0.5]"}).status_code == 422
    assert (
        post({"device_ocr_text": "a\nb", "device_ocr_confidences": "[0.5, 1]"}).status_code == 202
    )


def test_device_ocr_confidences_reach_the_comparison(client, member):
    scores = [0.9] * len(CARD)
    scores[3] = 0.1  # "1 cup water"
    status = import_photo(client, member, "\n".join(CARD), scores)
    assert "device_ocr_unsure" in status["result"]["ingredients"][1]["reason_codes"]


# ------------------------------------------------------------------ finalizing


def test_low_items_block_finalizing_until_confirmed_or_edited(client, member, sm):  # noqa: F811
    status = misread_card(client, member)
    honey = ("ingredient", 0)

    # Nothing decided: low and check items are listed.
    refused = finalize(client, member, status)
    assert refused.status_code == 409
    unresolved = refused.json()["detail"]["unresolved"]
    assert {x["confidence"] for x in unresolved} == {"low", "check"}
    assert {"target": "ingredient", "index": 0, "confidence": "low"}.items() <= unresolved[
        [x.get("index") for x in unresolved].index(0)
    ].items()

    # "accepted" is not enough for a low item.
    accepted = {"target": "ingredient", "index": 0, "action": "accepted"}
    refused = finalize(client, member, status, accepted, *settle_rest(status, {honey}))
    assert refused.status_code == 409
    assert [x["index"] for x in refused.json()["detail"]["unresolved"]] == [0]
    with sm() as session:
        assert session.get(Recipe, uuid.UUID(status["recipe_id"])).review_state == "needs_review"

    # Edit the misread amount: the phone was right, it's 1/2 tsp.
    edit = {
        "target": "ingredient",
        "index": 0,
        "action": "edited",
        "confidence": "low",
        "ingredient": {"quantity": 0.5, "unit": "tsp", "name": "honey"},
    }
    done = finalize(client, member, status, edit, *settle_rest(status, {honey}))
    assert done.status_code == 200, done.text
    job = done.json()
    assert job["id"] == status["id"]
    assert job["needs_review"] is False and job["review"]["state"] == "reviewed"
    assert job["review"]["outcomes"]["edited"] == 1

    with sm() as session:
        recipe = session.get(Recipe, uuid.UUID(status["recipe_id"]))
        assert recipe.review_state == "reviewed" and not recipe.is_draft
        assert recipe.review["reviewed_by"] == str(member.user_id)
        line = session.scalars(
            select(RecipeIngredient)
            .where(RecipeIngredient.recipe_id == recipe.id)
            .order_by(RecipeIngredient.position)
        ).first()
        assert float(line.quantity) == 0.5
        assert line.raw_text == "1/8 tsp honey"  # the original line is kept forever
        assert line.review["outcome"] == "edited"
        assert line.confidence == "low"  # the import's evidence stays as it was
        corrected = session.scalars(
            select(LearningEvent).where(
                LearningEvent.household_id == member.household_id,
                LearningEvent.event_type == "import_corrected",
            )
        ).all()
        assert len(corrected) == 1  # unit and name were unchanged
        payload = corrected[0].payload
        assert payload["field"] == "quantity" and payload["index"] == 0
        assert (payload["original"], payload["edited"]) == (0.125, 0.5)
        assert "readings_number" in payload["reason_codes"]
        assert payload["confidence"] == "low" and payload["source_kind"] == "photo"
        assert payload["raw_text"] == "1/8 tsp honey"
        assert payload["import_id"] == status["id"]
        assert corrected[0].subject_id == line.id and corrected[0].user_id == member.user_id
        summary = session.scalars(
            select(LearningEvent).where(
                LearningEvent.event_type == "import_reviewed",
                LearningEvent.subject_id == recipe.id,
            )
        ).one()
        outcomes = {(x["target"], x["outcome"]) for x in summary.payload["items"]}
        assert ("ingredient", "edited") in outcomes and ("ingredient", "accepted") in outcomes

    # Finalizing twice is refused.
    assert finalize(client, member, status, *settle_rest(status)).status_code == 409


def test_confirming_a_low_item_is_a_learning_event(client, member, sm):  # noqa: F811
    status = misread_card(client, member)
    done = finalize(client, member, status, *settle_rest(status))
    assert done.status_code == 200, done.text
    with sm() as session:
        confirmed = session.scalars(
            select(LearningEvent).where(
                LearningEvent.event_type == "import_confirmed",
                LearningEvent.payload["index"].as_integer() == 0,
                LearningEvent.household_id == member.household_id,
            )
        ).one()
        assert confirmed.payload["confidence"] == "low"


def test_an_edit_that_changes_nothing_counts_as_a_confirmation(client, member):
    status = misread_card(client, member)
    same = {
        "target": "ingredient",
        "index": 0,
        "action": "edited",
        "ingredient": {"quantity": 0.125, "unit": "teaspoon"},
    }
    done = finalize(client, member, status, same, *settle_rest(status, {("ingredient", 0)}))
    assert done.status_code == 200, done.text
    outcomes = done.json()["review"]["outcomes"]
    assert "edited" not in outcomes and outcomes["confirmed"] >= 1


def test_ingredient_name_edit_links_the_catalog(client, member, sm):  # noqa: F811
    with sm() as session:
        clover = Ingredient(household_id=member.household_id, name="clover honey")
        session.add(clover)
        session.commit()
        clover_id = clover.id
    status = misread_card(client, member)
    edit = {
        "target": "ingredient",
        "index": 0,
        "action": "edited",
        "ingredient": {"name": "Clover Honey", "alternatives": ["sugar"], "optional": True},
    }
    done = finalize(client, member, status, edit, *settle_rest(status, {("ingredient", 0)}))
    assert done.status_code == 200, done.text
    with sm() as session:
        line = session.scalars(
            select(RecipeIngredient).where(
                RecipeIngredient.recipe_id == uuid.UUID(status["recipe_id"]),
                RecipeIngredient.position == 0,
            )
        ).one()
        assert line.ingredient_id == clover_id
        assert line.review["edited"]["alternatives"] == ["sugar"]
        fields = set(
            session.scalars(
                select(LearningEvent.payload["field"].as_string()).where(
                    LearningEvent.event_type == "import_corrected",
                    LearningEvent.subject_id == line.id,
                )
            )
        )
        assert fields == {"name", "alternatives", "optional"}


def test_sections_and_steps_can_be_edited(client, member, sm):  # noqa: F811
    status = misread_card(client, member)
    skip = {("section", "title"), ("section", "yield"), ("section", "times"), ("step", 0)}
    done = finalize(
        client,
        member,
        status,
        {"target": "section", "field": "title", "action": "edited", "value": "Honey Tea for One"},
        {"target": "section", "field": "yield", "action": "edited", "value": "Serves 1"},
        {"target": "section", "field": "times", "action": "edited", "value": {"total_minutes": 5}},
        {"target": "step", "index": 0, "action": "edited", "text": "Steep 5 minutes."},
        *settle_rest(status, skip),
    )
    assert done.status_code == 200, done.text
    with sm() as session:
        recipe = session.get(Recipe, uuid.UUID(status["recipe_id"]))
        assert recipe.title == "Honey Tea for One"
        assert (recipe.yield_text, float(recipe.yield_quantity)) == ("Serves 1", 1.0)
        assert recipe.total_time_minutes == 5
        assert recipe.review["sections"]["title"]["outcome"] == "edited"
        step = session.scalars(select(RecipeStep).where(RecipeStep.recipe_id == recipe.id)).one()
        assert step.text == "Steep 5 minutes."
        fields = {
            e.payload["field"]
            for e in session.scalars(
                select(LearningEvent).where(
                    LearningEvent.event_type == "import_corrected",
                    LearningEvent.payload["recipe_id"].as_string() == str(recipe.id),
                )
            )
        }
        assert fields >= {"title", "yield_text", "yield_quantity", "total_minutes", "text"}


def test_html_import_with_only_check_items(client, member, sm):  # noqa: F811
    status = client.get(
        f"/v1/imports/{post_page(client, member).json()['id']}", headers=member.headers
    ).json()
    assert status["review"]["counts"]["low"] == 0
    assert finalize(client, member, status).status_code == 409
    done = finalize(client, member, status, *settle_rest(status))
    assert done.status_code == 200, done.text
    assert done.json()["review"]["outcomes"]["accepted"] >= 1


@pytest.mark.parametrize(
    "decision",
    [
        {"target": "ingredient", "index": 99, "action": "confirmed"},
        {"target": "ingredient", "action": "confirmed"},
        {"target": "ingredient", "index": 0, "field": "title", "action": "confirmed"},
        {"target": "ingredient", "index": 0, "action": "edited"},
        {"target": "ingredient", "index": 0, "action": "confirmed", "text": "x"},
        {"target": "ingredient", "index": 0, "action": "edited", "text": "1 cup"},
        {"target": "ingredient", "index": 0, "action": "edited", "ingredient": {"quantity": -1}},
        {"target": "ingredient", "index": 0, "action": "edited", "ingredient": {"colour": "x"}},
        {"target": "step", "index": 0, "action": "edited", "text": ""},
        {"target": "section", "field": "title", "action": "edited", "value": "  "},
        {"target": "section", "field": "times", "action": "edited", "value": "soon"},
        {"target": "section", "field": "flavour", "action": "confirmed"},
        {"target": "recipe", "index": 0, "action": "confirmed"},
        {"target": "ingredient", "index": 0, "action": "skipped"},
    ],
)
def test_finalize_request_validation(client, member, decision):
    status = misread_card(client, member)
    rest = settle_rest(status, {("ingredient", 0)})
    assert finalize(client, member, status, decision, *rest).status_code == 422


def test_duplicate_decisions_are_refused(client, member):
    status = misread_card(client, member)
    twice = {"target": "ingredient", "index": 0, "action": "confirmed"}
    rest = settle_rest(status, {("ingredient", 0)})
    assert finalize(client, member, status, twice, twice, *rest).status_code == 422


def test_finalize_is_private_to_the_household(client, member, sm):  # noqa: F811
    status = misread_card(client, member)
    other = create_member(sm, authed_settings(), name="Other cook")
    assert finalize(client, other, status, *settle_rest(status)).status_code == 404
    url = f"/v1/imports/{status['id']}/finalize"
    assert client.post(url, json={"decisions": []}).status_code == 401


# ------------------------------------------------------------------ sync


def test_sync_cannot_skip_the_review_or_write_confidence(client, member, sm):  # noqa: F811
    status = misread_card(client, member)
    recipe_id = status["recipe_id"]
    with sm() as session:
        line_id = session.scalars(
            select(RecipeIngredient.id).where(RecipeIngredient.recipe_id == uuid.UUID(recipe_id))
        ).first()

    def upload(*entries):
        return client.post("/v1/sync/upload", json={"batch": list(entries)}, headers=member.headers)

    def patch(table, row_id, **data):
        return {"op": "PATCH", "table": table, "id": str(row_id), "data": data}

    assert upload(patch("recipe", recipe_id, is_draft=False)).status_code == 422
    assert upload(patch("recipe", recipe_id, review_state="reviewed")).status_code == 422
    assert upload(patch("recipe_ingredient", line_id, confidence="high")).status_code == 422
    assert upload(patch("recipe_ingredient", line_id, review={})).status_code == 422
    assert upload(patch("recipe", recipe_id, notes="for colds")).status_code == 200
    with sm() as session:
        recipe = session.get(Recipe, uuid.UUID(recipe_id))
        assert recipe.is_draft and recipe.review_state == "needs_review"
        # A draft that never needed review can still be finalized through sync.
        plain = Recipe(household_id=member.household_id, title="Draft", is_draft=True)
        session.add(plain)
        session.commit()
        plain_id = plain.id
    assert upload(patch("recipe", plain_id, is_draft=False)).status_code == 200
