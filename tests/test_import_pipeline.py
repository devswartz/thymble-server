"""Extraction and the pipeline end to end without a database (LLM and HTTP are mocked)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from tests.conftest import make_settings
from tests.llm_fakes import FakeProvider
from thymble.importer.extract import extract_from_html, extract_from_text, html_to_text
from thymble.importer.pipeline import ImportFailed, ImportInput, ImportPipeline
from thymble.llm import LLMClient, MemoryStore

FIXTURES = Path(__file__).parent / "fixtures" / "import"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


def pipeline(provider: FakeProvider | None = None, transport=None, **settings) -> ImportPipeline:
    providers = {provider.name: provider} if provider else {}
    llm = LLMClient(make_settings(**settings), providers=providers, store=MemoryStore())
    return ImportPipeline(make_settings(**settings), llm, fetch_transport=transport)


RECIPE_JSON = {
    "is_recipe": True,
    "title": "Oat Drop Cookies",
    "description": None,
    "yield_text": "Makes about 24",
    "prep_time_minutes": None,
    "cook_time_minutes": 12,
    "total_time_minutes": None,
    "language": "en",
    "ingredient_groups": [
        {
            "name": None,
            "lines": [
                "1 cup butter, softened",
                "1 cup brown sugar",
                "2 eggs",
                "3 cups rolled oats",
            ],
        }
    ],
    "steps": [{"section": None, "text": "Cream, mix and bake 12 minutes."}],
}
LINE_PARSE = {
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


# ------------------------------------------------------------------ extraction


def test_jsonld_graph_with_sections_and_raw_values():
    draft = extract_from_html(page("jsonld_graph.html"), "https://example.test/lentil-soup")
    assert draft is not None and draft.complete
    assert draft.title == "Weeknight Lentil Soup"
    assert draft.language == "en"
    assert draft.image_url == "https://example.test/img/soup.jpg"
    assert draft.yield_raw == ["6", "6 bowls"]
    assert (draft.prep_time_raw, draft.total_time_raw) == ("PT15M", "PT55M")
    assert draft.ingredients[0][0].endswith("2 Tbsps extra virgin olive oil")
    assert draft.ingredients[-1] == ("salt & pepper to taste", None)
    assert draft.steps[0] == ("Warm the oil in a large pot over medium heat.", "Start the soup")
    assert draft.steps[-1][1] == "Simmer"
    assert draft.field_sources["title"] == "jsonld"


def test_page_without_markup_has_no_draft_and_text_is_cleaned():
    html = page("no_markup.html")
    assert extract_from_html(html, "https://example.test/notes") is None
    text = html_to_text(html)
    assert "Oat Drop Cookies" in text and "analytics" not in text and "Home | About" not in text


def test_extract_from_text_with_headings():
    text = (
        "Quick Pickles\nCrunchy and tangy.\n\nIngredients\n- 1 cucumber\nBrine:\n"
        "- 1 cup vinegar\n- 1 tbsp salt\n\nInstructions\n1. Slice the cucumber.\n"
        "2. Pour the brine over and chill 1 hour.\n"
    )
    draft = extract_from_text(text)
    assert draft is not None
    assert draft.title == "Quick Pickles"
    assert draft.description == "Crunchy and tangy."
    assert draft.ingredients == [
        ("1 cucumber", None),
        ("1 cup vinegar", "Brine"),
        ("1 tbsp salt", "Brine"),
    ]
    assert [s for s, _ in draft.steps] == [
        "Slice the cucumber.",
        "Pour the brine over and chill 1 hour.",
    ]
    assert extract_from_text("just a note about dinner") is None


# ------------------------------------------------------------------ pipeline: HTML


def test_device_html_import_without_llm():
    item = ImportInput(
        kind="html", html=page("jsonld_graph.html"), final_url="https://example.test/lentil-soup"
    )
    outcome = run(pipeline().run(item))
    result = outcome.to_result()
    assert result["final_url"] == "https://example.test/lentil-soup"
    assert result["yield"]["quantity"] == 6.0 and result["yield"]["unit"] == "bowls"
    assert result["times"] == {"prep_minutes": 15, "cook_minutes": 40, "total_minutes": 55}
    lines = outcome.lines
    assert [x.raw_text for x in lines][:2] == [
        "\u25a2 2 Tbsps extra virgin olive oil",
        "1 large onion, diced",
    ]
    first = lines[0]
    assert (first.quantity, first.unit, first.ingredient_name) == (
        2.0,
        "tbsp",
        "extra-virgin olive oil",
    )
    # "salt & pepper" became two lines with the same raw text.
    assert [x.name for x in lines[-2:]] == ["salt", "pepper"]
    assert lines[-1].raw_text == lines[-2].raw_text == "salt & pepper to taste"
    # The "or" line wanted the LLM; without one it is flagged for review instead of failing.
    broth = next(x for x in lines if x.raw_text.startswith("6 cups"))
    assert broth.needs_review and "no LLM provider configured" in broth.review_reasons[0]
    assert outcome.needs_review
    # Steps keep their HowToSection names and get timers.
    assert outcome.steps[1]["group"] == "Start the soup"
    assert outcome.steps[1]["timers"] == [
        {"label": "5-7 minutes", "seconds": 300, "seconds_max": 420}
    ]


def test_routed_lines_use_t3():
    provider = FakeProvider("local", lambda req: LINE_PARSE)
    item = ImportInput(kind="html", html=page("jsonld_graph.html"), final_url="https://e.test/x")
    outcome = run(pipeline(provider).run(item))
    broth = next(x for x in outcome.lines if x.raw_text.startswith("6 cups"))
    assert (broth.source, broth.unit, broth.alternatives) == ("llm", "cup", ["water"])
    assert not broth.needs_review
    assert [r.schema_name for r in provider.requests] == ["t3_lineparse"]
    assert "6 cups vegetable broth or water" in provider.requests[0].prompt


def test_headings_returned_as_steps_become_groups():
    item = ImportInput(
        kind="html", html=page("headings_as_steps.html"), final_url="https://e.test/c"
    )
    outcome = run(pipeline().run(item))
    assert [(s["text"], s["group"]) for s in outcome.steps] == [
        ("Heat the oven to 425F.", "Prepare the vegetables"),
        ("Toss the potatoes with the oil.", "Prepare the vegetables"),
        ("Add the chicken and roast for 35 to 40 minutes.", "Roast"),
    ]
    assert outcome.yield_info["quantity"] == 4.0


def test_page_without_markup_uses_t1():
    provider = FakeProvider("anthropic", lambda req: RECIPE_JSON)
    item = ImportInput(kind="html", html=page("no_markup.html"), final_url="https://e.test/n")
    outcome = run(pipeline(provider).run(item))
    assert outcome.draft.title == "Oat Drop Cookies"
    assert outcome.draft.extracted_by == ["llm:T1"]
    assert "extracted by AI" in outcome.review_reasons[0]
    request = provider.requests[0]
    assert request.schema_name == "t1_recipejson" and request.hard
    assert "<page_text>" in request.prompt and "analytics" not in request.prompt


def test_page_without_markup_and_without_llm_fails_clearly():
    item = ImportInput(kind="html", html=page("no_markup.html"), final_url="https://e.test/n")
    with pytest.raises(ImportFailed, match="no LLM provider configured"):
        run(pipeline().run(item))


# ------------------------------------------------------------------ pipeline: text and photo


def test_text_import_uses_rules_when_headings_exist():
    text = (
        "Toast\n\nIngredients\n2 slices bread\n1 tbsp butter\n\nMethod\nToast the bread 3 minutes."
    )
    outcome = run(pipeline().run(ImportInput(kind="text", text=text)))
    assert outcome.draft.extracted_by == ["text-rules"]
    assert [x.name for x in outcome.lines] == ["bread", "butter"]


def test_text_import_without_headings_uses_t2():
    provider = FakeProvider("anthropic", lambda req: RECIPE_JSON)
    outcome = run(pipeline(provider).run(ImportInput(kind="text", text="cookies: butter, oats")))
    assert outcome.draft.title == "Oat Drop Cookies"
    assert provider.requests[0].schema_name == "t2_recipejson"


def test_photo_import_two_passes_with_ditto_between():
    transcription = {
        "lines": [
            "Spice Cake",
            "Ingredients",
            "1 cup sugar",
            "do.",
            "2 tsp cinnamon",
            '1 "',
            "Method",
            "Mix and bake 40 minutes.",
        ]
    }

    def reply(request):
        assert request.schema_name == "t2_transcription"
        assert request.images and request.images[0].media_type == "image/jpeg"
        return transcription

    provider = FakeProvider("anthropic", reply)
    item = ImportInput(kind="photo", photo=b"\xff\xd8fake", photo_mime="image/jpeg")
    outcome = run(pipeline(provider).run(item))
    assert outcome.transcription == transcription["lines"]  # literal, kept for review
    assert [x.raw_text for x in outcome.lines] == [
        "1 cup sugar",
        "1 cup sugar",
        "2 tsp cinnamon",
        "1 tsp cinnamon",
    ]
    assert outcome.lines[3].quantity == 1.0 and outcome.lines[3].unit == "tsp"
    assert outcome.steps[0]["timers"][0]["seconds"] == 2400
    assert len(provider.requests) == 1  # headings found: structuring pass done by rules


def test_photo_without_headings_runs_the_structuring_pass():
    replies = [{"lines": ["Cookies", "butter", "oats", "bake"]}, RECIPE_JSON]
    provider = FakeProvider("local", replies)
    item = ImportInput(kind="photo", photo=b"png", photo_mime="image/png")
    outcome = run(pipeline(provider).run(item))
    assert outcome.draft.title == "Oat Drop Cookies"
    second = provider.requests[1]
    assert second.schema_name == "t2_recipejson" and not second.images
    assert "Cookies\nbutter\noats\nbake" in second.prompt


def test_photo_rejects_other_types():
    item = ImportInput(kind="photo", photo=b"%PDF", photo_mime="application/pdf")
    with pytest.raises(ImportFailed, match="JPEG"):
        run(pipeline(FakeProvider("anthropic", [])).run(item))


# ------------------------------------------------------------------ pipeline: server fetch


def transport_for(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("ThymbleBot/")
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.MockTransport(handler)


def test_url_import_follows_redirects_and_decodes_without_charset():
    body = page("jsonld_graph.html").encode("utf-8")
    routes = {
        "https://example.test/old": httpx.Response(
            301, headers={"location": "https://example.test/lentil-soup"}
        ),
        "https://example.test/lentil-soup": httpx.Response(
            200, content=body, headers={"content-type": "text/html"}
        ),
    }
    item = ImportInput(kind="url", url="https://example.test/old")
    outcome = run(pipeline(transport=transport_for(routes)).run(item))
    assert outcome.final_url == "https://example.test/lentil-soup"
    assert outcome.draft.source_url == "https://example.test/lentil-soup"
    assert "redirected to a different page" in outcome.review_reasons[0]
    assert outcome.stages[0] == "fetch"


def test_url_import_respects_robots_txt():
    routes = {
        "https://example.test/robots.txt": httpx.Response(
            200, text="User-agent: *\nDisallow: /private/\n"
        ),
    }
    item = ImportInput(kind="url", url="https://example.test/private/recipe")
    with pytest.raises(ImportFailed, match=r"robots\.txt"):
        run(pipeline(transport=transport_for(routes)).run(item))


def test_url_import_reports_bot_protection_and_does_not_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403, text="<title>Just a moment...</title> cf-chl")

    item = ImportInput(kind="url", url="https://example.test/recipe")
    with pytest.raises(ImportFailed, match=r"Cloudflare.*share it"):
        run(pipeline(transport=httpx.MockTransport(handler)).run(item))
    assert calls == ["https://example.test/robots.txt", "https://example.test/recipe"]


def test_url_import_refuses_private_addresses():
    item = ImportInput(kind="url", url="http://127.0.0.1:8000/healthz")
    with pytest.raises(ImportFailed, match="public internet"):
        run(pipeline().run(item))


def test_url_import_can_be_switched_off():
    item = ImportInput(kind="url", url="https://example.test/x")
    with pytest.raises(ImportFailed, match="turned off"):
        run(pipeline(import_server_fetch_enabled=False).run(item))
