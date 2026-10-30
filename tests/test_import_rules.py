"""Deterministic rules: no parser model, no LLM, no network. Lines are written for these tests."""

from __future__ import annotations

import pytest

from thymble.importer import rules
from thymble.importer.fetch import blocked_reason, decode_html


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("Tbsps", "tbsp"),
        ("tablespoons", "tbsp"),
        ("T", "tbsp"),
        ("t", "tsp"),
        ("tsp.", "tsp"),
        ("pounds", "lb"),
        ("fluid_ounce", "fl oz"),
        ("fl. oz.", "fl oz"),
        ("pints", "pt"),
        ("Quarts", "qt"),
        ("pieces", "piece"),
        ("tins", "can"),
        ("cloves", "clove"),
        ("smidgen", "smidgen"),
        (None, None),
    ],
)
def test_unit_aliases(raw, code):
    assert rules.norm_unit(raw) == code


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("a dozen eggs", "12 eggs"),
        ("half a dozen rolls", "6 rolls"),
        ("2 dozen cookies", "24 cookies"),
        ("two cloves garlic", "2 cloves garlic"),
        ("a pinch of nutmeg", "1 pinch of nutmeg"),
        ("a few sprigs thyme", "a few sprigs thyme"),
        ("2 15 oz cans beans", "2 (15 oz) cans beans"),
        ("1 x 400g tin tomatoes", "1 (400g) tin tomatoes"),
    ],
)
def test_word_numbers(line, expected):
    assert rules.word_numbers(line) == expected


def test_plus_minus():
    assert rules.plus_minus("2 cups minus 2 tablespoons sugar") == "1.875 cups sugar"
    assert rules.plus_minus("1 cup plus 1 tbsp milk") == "1.0625 cup milk"
    assert rules.plus_minus("1 lb plus 2 oz beef") == "1 lb plus 2 oz beef"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("\u25a2 1 cup rice", "1 cup rice"),
        ("2 carrots (, thinly sliced (Note 2))", "2 carrots (thinly sliced)"),
        ("3 undefined large eggs", "3 large eggs"),
        ("4 chicken thighs (boneless) 2", "4 chicken thighs (boneless)"),
        ("1 \u2014 2 teaspoons chili flakes", "1-2 teaspoons chili flakes"),
        ("tahini*", "tahini"),
        ("1/2 cucumber.", "1/2 cucumber"),
        ("do.", "do."),
    ],
)
def test_clean_line(line, expected):
    assert rules.clean_line(line) == expected


def test_lang_normalize_french_and_italian():
    text, _ = rules.lang_normalize("3 cuillères à soupe de sucre", "fr")
    assert text == "3 tbsp sucre"
    text, info = rules.lang_normalize("Farina 250 g", "it")
    assert text == "250 g Farina"
    text, info = rules.lang_normalize("Sale q.b.", "it")
    assert info["to_taste"] is True


@pytest.mark.parametrize(
    ("line", "kind", "arg"),
    [
        ("do.", "all", None),
        ("same as above", "all", None),
        ('"', "all", None),
        ("2 cups do.", "qty_unit", "2 cups"),
        ('3 "', "qty", "3"),
        ('" nutmeg', "name", "nutmeg"),
        ("1 cup dough", None, None),
    ],
)
def test_ditto_kind(line, kind, arg):
    assert rules.ditto_kind(line) == (kind, arg)


def test_resolve_ditto_lines_copies_quantity_and_unit():
    lines = ["1 cup sugar", "do.", "2 cups do.", '3 "', '" brown sugar', "4 eggs", "same as above"]
    assert rules.resolve_ditto_lines(lines) == [
        "1 cup sugar",
        "1 cup sugar",
        "2 cups sugar",
        "3 cups sugar",
        "3 cups brown sugar",
        "4 eggs",
        "4 eggs",
    ]
    assert rules.resolve_ditto_lines(["do.", "1 egg"]) == ["do.", "1 egg"]


@pytest.mark.parametrize(
    ("raw", "quantity", "quantity_max", "unit"),
    [
        (["48", "4 dozen cookies"], 48.0, None, "cookies"),
        ("Serves 4", 4.0, None, "servings"),
        ("4-6 servings", 4.0, 6.0, "servings"),
        ("Makes 12 muffins", 12.0, None, "muffins"),
        (8, 8.0, None, "servings"),
        ("one 9-inch pie", 1.0, None, "pie"),
        (None, None, None, None),
    ],
)
def test_parse_yield(raw, quantity, quantity_max, unit):
    result = rules.parse_yield(raw)
    assert (result["quantity"], result["quantity_max"], result["unit"]) == (
        quantity,
        quantity_max,
        unit,
    )


@pytest.mark.parametrize(
    ("raw", "minutes"),
    [("PT15M", 15), ("PT1H30M", 90), ("P0DT0H25M", 25), ("PT45S", 1), ("P1D", 1440), ("", None)],
)
def test_parse_duration(raw, minutes):
    assert rules.parse_duration(raw) == minutes


def test_split_steps_turns_headings_into_groups_and_merges_broken_steps():
    steps = [
        ("For the sauce:", None),
        ("Whisk the soy sauce and honey,", None),
        ("then set aside.", None),
        ("Cook the noodles", None),
        ("Boil the noodles for 8 minutes.", None),
        ("Drain and toss with the sauce.", None),
    ]
    assert rules.split_steps(steps) == [
        ("Whisk the soy sauce and honey, then set aside.", "For the sauce"),
        ("Boil the noodles for 8 minutes.", "Cook the noodles"),
        ("Drain and toss with the sauce.", "Cook the noodles"),
    ]


def test_split_steps_keeps_existing_groups_and_short_last_step():
    steps = [("Mix everything.", "Batter"), ("Serve warm", None)]
    assert rules.split_steps(steps) == [("Mix everything.", "Batter"), ("Serve warm", None)]


def test_detect_timers():
    assert rules.detect_timers("Bake for 25 minutes.") == [{"label": "25 minutes", "seconds": 1500}]
    assert rules.detect_timers("Simmer 1 1/2 hours.") == [{"label": "1 1/2 hours", "seconds": 5400}]
    assert rules.detect_timers("Roast for 35 to 40 minutes.") == [
        {"label": "35 to 40 minutes", "seconds": 2100, "seconds_max": 2400}
    ]
    assert rules.detect_timers("Rest for half an hour.")[0]["seconds"] == 1800
    assert rules.detect_timers("Marinate up to 2 hours.") == []
    assert rules.detect_timers("Stir well.") == []


def test_detect_language():
    assert (
        rules.detect_language("Mix the flour and the sugar with the eggs in a bowl until") == "en"
    )
    german = "Die Butter mit dem Zucker und den Eiern in einer Schüssel verrühren bis"
    assert rules.detect_language(german) == "de"
    assert rules.detect_language("short") == "en"


def test_clean_title():
    assert rules.clean_title("Lentil Soup | My Blog") == "Lentil Soup"
    assert rules.clean_title("Lentil Soup Recipe by Someone") == "Lentil Soup"
    assert rules.clean_title("Lentil Soup - Cozy and quick", h1="Lentil Soup") == "Lentil Soup"


def test_decode_html_honours_charsets():
    text = "Crème brûlée"
    assert decode_html(text.encode("utf-8"), "text/html") == text  # no charset: not Latin-1
    assert decode_html(text.encode("latin-1"), "text/html; charset=ISO-8859-1") == text
    meta = f'<meta charset="windows-1252"><p>{text}</p>'.encode("cp1252")
    assert text in decode_html(meta, None)


def test_blocked_reason():
    assert "Cloudflare" in blocked_reason(403, "<title>Just a moment...</title>")
    assert "429" in blocked_reason(429, "")
    assert blocked_reason(200, "<html>") is None
    assert "404" in blocked_reason(404, "")
