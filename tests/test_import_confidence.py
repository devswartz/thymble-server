"""Import confidence (D-034, D-035): every line, step and section gets high / check / low with
reasons built from evidence. Recipe text here is written for these tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from tests.conftest import make_settings
from tests.llm_fakes import FakeProvider
from thymble.importer import confidence
from thymble.importer.confidence import (
    Assessment,
    assess_line,
    assess_step,
    implausible_amount,
    looks_like_heading,
    redirect_reason,
)
from thymble.importer.extract import RecipeDraft, merge
from thymble.importer.ingredients import ParsedLine, parse_line
from thymble.importer.pipeline import ImportInput, ImportPipeline
from thymble.importer.thresholds import REASONS, Level
from thymble.llm import LLMClient, MemoryStore

FIXTURES = Path(__file__).parent / "fixtures" / "import"


def run(coro):
    return asyncio.run(coro)


def pipeline(provider: FakeProvider | None = None, transport=None) -> ImportPipeline:
    providers = {provider.name: provider} if provider else {}
    llm = LLMClient(make_settings(), providers=providers, store=MemoryStore())
    return ImportPipeline(make_settings(), llm, fetch_transport=transport)


def line(**kw) -> ParsedLine:
    base = {"raw_text": "1 cup sugar", "text": "1 cup sugar", "quantity": 1.0, "unit": "cup"}
    base.update({"name": "sugar", "parse_score": 0.99})
    base.update(kw)
    return ParsedLine(**base)


# ------------------------------------------------------------------ the reason table


def test_every_reason_has_a_level_and_plain_text():
    for code, (level, text) in REASONS.items():
        assert isinstance(level, Level), code
        # Reads as part of a sentence in the app ("Check: the AI had to parse this line").
        assert text and (text[0].islower() or text.startswith("{")), code


def test_level_is_the_most_severe_reason():
    a = Assessment()
    assert a.level == Level.HIGH and a.to_dict()["reasons"] == []
    a.add("llm_parsed")
    assert a.level == Level.CHECK
    a.add("photo_unreadable")
    a.add("ditto_copied")
    assert a.level == Level.LOW
    a.add("llm_parsed")  # no duplicates
    assert a.codes == ["llm_parsed", "photo_unreadable", "ditto_copied"]


def test_reason_details_are_filled_in():
    a = Assessment()
    a.add("readings_number", "1/8 vs 1/2")
    assert a.reasons == ["the two readings of the photo differ in a number: 1/8 vs 1/2"]


# ------------------------------------------------------------------ plausibility


@pytest.mark.parametrize(
    ("quantity", "unit", "name", "bad"),
    [
        (15, "cup", "salt", True),
        (1, "cup", "kosher salt", False),  # brines exist
        (2, "cup", "kosher salt", True),
        (2, "tsp", "salt", False),
        (5, "tbsp", "baking soda", True),
        (1, "tbsp", "baking powder", False),
        (3, "cup", "all-purpose flour", False),
        (30, "cup", "all-purpose flour", True),
        (2, "cup", "chopped bell pepper", False),
        (1, "cup", "ground black pepper", True),
        (2, "lb", "chicken thighs", False),
        (40, "lb", "chicken thighs", True),
        (500, "g", "salt", True),
        (10, "g", "salt", False),
        (3, None, "eggs", False),
        (300, None, "eggs", True),
        (0, "cup", "milk", True),
        (2, "tablespoons", "vanilla extract", False),
        (8, "tablespoons", "vanilla extract", True),
        (None, None, "salt", False),
        (3, "bushel", "apples", False),  # unknown units are not judged here
    ],
)
def test_implausible_amounts(quantity, unit, name, bad):
    assert (implausible_amount(quantity, None, unit, name) is not None) == bad


def test_implausible_amount_wording_and_ranges():
    assert implausible_amount(15, None, "cup", "salt") == "15 cup of salt"
    assert implausible_amount(3, 2, "cup", "milk") == "3-2 cup"  # max below min
    assert implausible_amount(1, 20, "cup", "milk") is not None  # upper bound judged


# ------------------------------------------------------------------ lines


def test_clean_line_is_high():
    a = assess_line(line())
    assert a.level == Level.HIGH and a.reasons == []


@pytest.mark.parametrize(
    ("kw", "code", "level"),
    [
        ({"parse_score": 0.85}, "parser_unsure", Level.CHECK),
        ({"parse_score": 0.3}, "parser_very_unsure", Level.LOW),
        ({"name": None}, "no_name", Level.LOW),
        ({"source": "llm"}, "llm_parsed", Level.CHECK),
        ({"ditto": True}, "ditto_copied", Level.CHECK),
        ({"alternatives": ["water"]}, "or_split", Level.CHECK),
        ({"flags": ["non-English"]}, "non_english", Level.CHECK),
        ({"quantity": 15.0, "name": "salt"}, "implausible_amount", Level.LOW),
        ({"unit": "smidgen"}, "unknown_unit", Level.CHECK),
        ({"resolved_by": "llm", "ingredient_label": "granulated sugar"}, "catalog_ai_match", "c"),
        ({"resolved_by": "new"}, "catalog_new", Level.CHECK),
        ({"raw_text": "1 cup [?] sugar"}, "photo_unreadable", Level.LOW),
        ({"raw_text": "1 cup sug[cut]"}, "photo_cut_off", Level.LOW),
        ({"review_reasons": ["check this line (or/sub; no LLM)"]}, "llm_failed", Level.CHECK),
    ],
)
def test_line_reasons(kw, code, level):
    a = assess_line(line(**kw))
    assert code in a.codes
    assert a.level == (Level.CHECK if level == "c" else level)


def test_llm_answer_never_raises_confidence():
    # The parser was very unsure; the LLM answered. Still not high.
    a = assess_line(line(parse_score=0.3, source="llm"))
    assert a.level == Level.CHECK
    assert {"parser_unsure", "llm_parsed"} <= set(a.codes)


def test_catalog_match_reason_names_the_match():
    a = assess_line(line(resolved_by="llm", ingredient_label="granulated sugar"))
    assert a.reasons == ['not in the catalog yet; the AI matched it to "granulated sugar"']


def test_catalog_hits_are_not_flagged():
    for how in ("catalog", "dictionary", None):
        assert assess_line(line(resolved_by=how)).level == Level.HIGH


def test_or_split_from_parsed_line():
    a = assess_line(parse_line("1 cup chicken stock or water"))
    assert "or_split" in a.codes


def test_sources_disagree_on_a_line():
    a = assess_line(line(raw_text="1 cup Sugar."), disagreeing={"1 cup sugar"})
    assert a.codes == ["sources_disagree"]


def test_ai_extracted_lines():
    assert assess_line(line(), ai_source="llm:T1").codes == ["ai_extracted"]
    assert assess_line(line(), ai_source="llm:T2").codes == ["ai_structured"]


# ------------------------------------------------------------------ steps


@pytest.mark.parametrize(
    ("text", "heading"),
    [
        ("Make the glaze", True),
        ("For the filling:", True),
        ("TO SERVE", True),
        ("Stir well.", False),
        ("Whisk the eggs with the sugar until pale, then fold in the flour", False),
        ("Serve warm (or cold).", False),
    ],
)
def test_step_heading_detection(text, heading):
    assert looks_like_heading(text) is heading
    assert ("step_maybe_heading" in assess_step(text).codes) is heading


# ------------------------------------------------------------------ redirect


def test_redirect_reasons():
    assert redirect_reason("https://a.test/x", "https://a.test/x/", "Anything") is None
    assert (
        redirect_reason("https://a.test/lemon-bars", "https://a.test/lemon-bars-2", "Lemon Bars")
        == "redirected"
    )
    assert (
        redirect_reason("https://a.test/lemon-bars", "https://b.test/r/123", "Beef Stew")
        == "redirect_title_mismatch"
    )
    # Nothing to compare the title with: just "redirected".
    assert redirect_reason("https://a.test/r/123", "https://b.test/x", "Stew") == "redirected"


# ------------------------------------------------------------------ reader disagreement


def test_merge_records_where_readers_disagree():
    a = RecipeDraft(
        title="Lemon Bars",
        yield_raw="16 bars",
        prep_time_raw="PT20M",
        ingredients=[("1 cup flour", None), ("2 eggs", None)],
        steps=[("Mix it all.", None), ("Bake 25 minutes.", None)],
        extracted_by=["jsonld"],
    )
    b = RecipeDraft(
        title="Lemon Squares",
        yield_raw="12 bars",
        prep_time_minutes=20,
        ingredients=[("1 cup flour", None), ("3 eggs", None)],
        steps=[("Mix it all. Bake 25 minutes.", None)],
        extracted_by=["site-scraper"],
    )
    found = merge([a, b]).disagreements
    assert found["title"] == ["lemon bars", "lemon squares"]
    assert found["yield"] == [12.0, 16.0]
    assert "times" not in found  # PT20M and 20 minutes agree
    assert found["ingredients"] == ["2 eggs"]
    assert "steps" not in found  # same text, split differently


def test_merge_of_agreeing_readers_has_no_disagreements():
    a = RecipeDraft(title="Toast", ingredients=[("1 slice bread", None)], extracted_by=["jsonld"])
    b = RecipeDraft(title="Toast", ingredients=[("1 slice bread", None)], extracted_by=["x"])
    assert merge([a, b]).disagreements == {}


# ------------------------------------------------------------------ whole imports


def test_device_html_import_scores_every_item():
    html = (FIXTURES / "jsonld_graph.html").read_text(encoding="utf-8")
    item = ImportInput(kind="html", html=html, final_url="https://example.test/lentil-soup")
    outcome = run(pipeline().run(item))
    result = outcome.to_result()
    by_text = {x["raw_text"]: x for x in result["ingredients"]}
    assert by_text["1 tsp ground cumin"]["confidence"] == "high"
    broth = by_text["6 cups vegetable broth or water"]
    assert broth["confidence"] == "check"
    assert broth["reason_codes"] == ["llm_failed", "or_split"]
    assert all(s["confidence"] == "high" for s in result["steps"])
    review = result["review"]
    assert review["state"] == "needs_review" and result["needs_review"] is True
    assert review["counts"]["check"] == 1 and review["counts"]["low"] == 0
    sections = {s["field"]: s for s in result["sections"]}
    assert list(sections) == ["title", "yield", "times", "groups"]
    assert sections["title"]["value"] == "Weeknight Lentil Soup"
    assert sections["groups"]["value"] == ["Simmer", "Start the soup"]
    assert sections["times"]["value"]["total_minutes"] == 55
    assert review["completeness"]["confidence"] == "high"
    assert by_text["1 tsp ground cumin"]["device_reading"] is None


def test_all_high_text_import_needs_no_review():
    text = (
        "Toast\n\nIngredients\n2 slices bread\n1 tbsp butter\n\nMethod\nToast the bread 3 minutes."
    )
    outcome = run(pipeline().run(ImportInput(kind="text", text=text)))
    # No yield in the text: the yield section asks for a check.
    assert outcome.sections["yield"]["reason_codes"] == ["missing_yield"]
    text = text.replace("Toast\n\n", "Toast\nServes 2\n\n", 1)
    outcome = run(pipeline().run(ImportInput(kind="text", text=text)))
    assert [x.confidence for x in outcome.lines] == ["high", "high"]


def test_implausible_amount_from_a_pasted_recipe_is_low():
    text = "Brine\n\nIngredients\n15 cups salt\n4 quarts water\n\nMethod\nDissolve the salt."
    outcome = run(pipeline().run(ImportInput(kind="text", text=text)))
    salt = outcome.lines[0]
    assert salt.confidence == "low"
    assert salt.reasons == ["15 cup of salt looks unlikely; check the amount and unit"]
    assert outcome.lines[1].confidence == "high"
    assert outcome.review_state == "needs_review"


def test_headings_among_steps_flag_the_groups_section():
    html = (FIXTURES / "headings_as_steps.html").read_text(encoding="utf-8")
    outcome = run(pipeline().run(ImportInput(kind="html", html=html, final_url="https://e.test/c")))
    assert outcome.sections["groups"]["reason_codes"] == ["heading_from_steps"]
    assert outcome.sections["groups"]["confidence"] == "check"


def test_url_redirect_to_another_recipe_makes_the_title_low():
    html = (FIXTURES / "jsonld_graph.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/chocolate-cake":
            return httpx.Response(301, headers={"location": "https://example.test/soup-of-day"})
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    item = ImportInput(kind="url", url="https://example.test/chocolate-cake")
    outcome = run(pipeline(transport=httpx.MockTransport(handler)).run(item))
    title = outcome.sections["title"]
    assert title["confidence"] == "low"
    assert title["reason_codes"] == ["redirect_title_mismatch"]


def test_page_extracted_by_the_llm_is_never_high():
    recipe = {
        "is_recipe": True,
        "title": "Oat Drops",
        "description": None,
        "yield_text": "Makes 24",
        "prep_time_minutes": None,
        "cook_time_minutes": 12,
        "total_time_minutes": None,
        "language": "en",
        "ingredient_groups": [{"name": None, "lines": ["1 cup rolled oats"]}],
        "steps": [{"section": None, "text": "Mix and bake 12 minutes."}],
    }
    provider = FakeProvider("anthropic", lambda req: recipe)
    html = (FIXTURES / "no_markup.html").read_text(encoding="utf-8")
    outcome = run(
        pipeline(provider).run(ImportInput(kind="html", html=html, final_url="https://e.test/n"))
    )
    assert all("ai_extracted" in x.reason_codes for x in outcome.lines)
    assert all("ai_extracted" in s["reason_codes"] for s in outcome.steps)
    assert outcome.sections["title"]["reason_codes"] == ["ai_extracted"]
    assert outcome.sections["times"]["reason_codes"] == ["ai_structured"]


# ------------------------------------------------------------------ photos (D-035)

CARD = [
    "Spice Cake",
    "Ingredients",
    "1 cup sugar",
    "do.",
    "1/8 tsp salt",
    "2 tsp cinnamon",
    "1 cup coarse sugar",
    "Method",
    "Mix and bake 40 minutes.",
]


def photo(lines: list[str], device: str | None = None, scores=None):
    provider = FakeProvider("local", lambda req: {"lines": lines})
    item = ImportInput(
        kind="photo",
        photo=b"\xff\xd8x",
        photo_mime="image/jpeg",
        device_ocr_text=device,
        device_ocr_confidences=scores,
    )
    return run(pipeline(provider).run(item))


def test_photo_without_a_second_reading_is_never_high():
    outcome = photo(CARD)
    assert outcome.readings is None
    assert all(x.confidence != "high" for x in outcome.lines)
    assert all("photo_single_reading" in x.reason_codes for x in outcome.lines)
    assert outcome.sections["title"]["reason_codes"] == ["photo_single_reading"]
    assert outcome.review_state == "needs_review"


def test_photo_readings_agree_disagree_and_ditto():
    device = "\n".join(CARD).replace("1/8 tsp", "1/2 tsp").replace("coarse", "course")
    outcome = photo(CARD, device)
    levels = {x.raw_text: (x.confidence, x.reason_codes) for x in outcome.lines[2:]}
    assert (outcome.lines[0].confidence, outcome.lines[0].reason_codes) == ("high", [])
    assert outcome.lines[1].reason_codes == ["ditto_copied"]  # "do." read by both
    assert outcome.lines[1].readings == {"model": "do.", "device": "do.", "agreement": "agree"}
    assert levels["1/8 tsp salt"] == ("low", ["readings_number"])
    assert outcome.lines[2].reasons == [
        "the two readings of the photo differ in a number: 1/8 vs 1/2"
    ]
    assert levels["2 tsp cinnamon"] == ("high", [])
    assert levels["1 cup coarse sugar"] == ("check", ["readings_text"])
    assert outcome.steps[0]["confidence"] == "high"
    assert outcome.sections["title"]["confidence"] == "high"
    # Even with everything agreeing, a photo import always goes through review.
    assert outcome.review_state == "needs_review"
    result = outcome.to_result()
    assert result["readings"]["model"] == CARD
    assert result["readings"]["device"][4] == "1/2 tsp salt"
    assert result["ingredients"][2]["readings"]["device"] == "1/2 tsp salt"
    assert "compare-readings" in result["stages"]


def test_photo_unit_disagreement_is_low():
    device = "\n".join(CARD).replace("2 tsp cinnamon", "2 tbsp cinnamon")
    outcome = photo(CARD, device)
    cinnamon = next(x for x in outcome.lines if x.name == "cinnamon")
    assert (cinnamon.confidence, cinnamon.reason_codes) == ("low", ["readings_unit"])


def test_photo_line_the_device_missed():
    device = "\n".join(line for line in CARD if line != "2 tsp cinnamon")
    outcome = photo(CARD, device)
    cinnamon = next(x for x in outcome.lines if x.name == "cinnamon")
    assert cinnamon.reason_codes == ["readings_missing_number"]
    assert cinnamon.confidence == "low"


def test_photo_line_the_model_dropped():
    device = "\n".join([*CARD[:6], "3 eggs", *CARD[6:]])
    outcome = photo(CARD, device)
    complete = outcome.sections["completeness"]
    assert complete["reason_codes"] == ["photo_lines_dropped"]
    assert complete["reasons"] == [
        "1 line(s) the phone read aren't in the recipe; check nothing is missing"
    ]


def test_photo_unreadable_and_cut_off_words_are_low():
    lines = [*CARD]
    lines[5] = "2 tsp [?]"
    lines[8] = "Mix and bake 40 min[cut]"
    outcome = photo(lines, "\n".join(lines))
    assert "photo_unreadable" in outcome.lines[3].reason_codes
    assert outcome.lines[3].confidence == "low"
    assert outcome.steps[0]["confidence"] == "low"
    assert "photo_cut_off" in outcome.steps[0]["reason_codes"]


def test_photo_device_ocr_unsure_lowers_agreeing_line():
    scores = [0.95] * len(CARD)
    scores[2] = 0.2
    outcome = photo(CARD, "\n".join(CARD), scores)
    assert outcome.lines[0].reason_codes == ["device_ocr_unsure"]
    assert outcome.lines[0].confidence == "check"


def test_photo_with_unicode_fractions_in_one_reading_agrees():
    lines = ["Milk Toast", "Ingredients", "1½ cups milk", "Method", "Warm the milk."]
    device = "Milk Toast\nIngredients\n1 1/2 cups milk\nMethod\nWarm the milk."
    outcome = photo(lines, device)
    assert outcome.lines[0].confidence == "high"


def test_report_counts():
    device = "\n".join(CARD).replace("1/8 tsp", "1/2 tsp")
    outcome = photo(CARD, device)
    counts = outcome.confidence_counts
    total = len(outcome.lines) + len(outcome.steps) + len(confidence.SECTIONS)
    assert sum(counts.values()) == total
    assert counts["low"] == 1
