"""Ingredient lines (rules + ingredient-parser-nlp) and the compound dictionary.

Lines are written for these tests; the parser model runs locally (no network).
"""

from __future__ import annotations

import pytest

from thymble.importer.ingredients import parse_line, parse_lines
from thymble.importer.normalize import apply_dictionary, lookup_compound, name_key, singular


def fields(line):
    return (line.quantity, line.quantity_max, line.unit, line.name, line.preparation)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2 cups all-purpose flour, sifted", (2.0, None, "cup", "all-purpose flour", "sifted")),
        ("1 cup packed light brown sugar", (1.0, None, "cup", "light brown sugar", "packed")),
        ("a dozen eggs", (12.0, None, None, "eggs", None)),
        ("2 Tbsps soy sauce", (2.0, None, "tbsp", "soy sauce", None)),
        ("1 1/2 lb. bone-in pork shoulder", (1.5, None, "lb", "bone-in pork shoulder", None)),
        ("2-3 tbsp maple syrup", (2.0, 3.0, "tbsp", "maple syrup", None)),
        ("1 lb 2 oz potatoes", (1.125, None, "lb", "potatoes", None)),
        ("1 cup half-and-half", (1.0, None, "cup", "half-and-half", None)),
    ],
)
def test_parse_line_fields(text, expected):
    line = parse_line(text)
    assert fields(line) == expected
    assert line.raw_text == text
    assert not line.route_to_llm


def test_and_means_separate_items():
    line = parse_line("salt & pepper to taste")
    assert line.name == "salt"
    assert line.additional == ["pepper"]
    assert line.alternatives == []
    assert line.note == "to taste"


def test_or_means_substitute_and_routes_to_llm():
    line = parse_line("1 cup chicken stock or water")
    assert (line.name, line.alternatives, line.additional) == ("chicken stock", ["water"], [])
    assert "or/sub" in line.flags and line.route_to_llm


def test_or_as_needed_is_not_an_alternative():
    line = parse_line("olive oil, or as needed")
    assert line.alternatives == []
    assert "or/sub" not in line.flags


def test_parenthetical_alias_moves_to_note():
    line = parse_line("1 bunch green onions (scallions), thinly sliced")
    assert (line.name, line.preparation, line.note) == (
        "green onions",
        "thinly sliced",
        "scallions",
    )


def test_non_english_lines_always_route_to_llm():
    line = parse_line("3 cuillères à soupe de sucre", lang="fr")
    assert (line.quantity, line.unit) == (3.0, "tbsp")
    assert line.flags == ["non-English"]


def test_ditto_lines_copy_from_the_previous_line():
    lines = parse_lines([("1 cup sugar", None), ("do.", None), ("2 cups do.", None)])
    assert [(x.quantity, x.unit, x.name, x.ditto) for x in lines] == [
        (1.0, "cup", "sugar", False),
        (1.0, "cup", "sugar", True),
        (2.0, "cup", "sugar", True),
    ]
    assert [x.raw_text for x in lines] == ["1 cup sugar", "do.", "2 cups do."]


def test_empty_after_cleaning_has_no_name():
    line = parse_line("\u25a2")
    assert line.flags == ["no name"]


# ------------------------------------------------------------------ compound dictionary


def test_name_key_and_singular():
    assert name_key("All-Purpose  Flour") == "all purpose flour"
    assert name_key("Confectioners' sugar") == "confectioners sugar"
    assert singular("tomatoes") == "tomato"
    assert singular("berries") == "berry"
    assert singular("molasses") == "molasses"


def test_lookup_compound_variants_and_synonyms():
    assert lookup_compound("all purpose flour").canonical == "all-purpose flour"
    assert lookup_compound("Scallions").canonical == "green onions"
    assert lookup_compound("caster sugar").canonical == "granulated sugar"
    assert lookup_compound("chicken thigh").canonical == "chicken thighs"
    assert lookup_compound("unicorn tears") is None


def test_dictionary_restores_a_split_compound():
    line = parse_line("2 cups all purpose flour")
    line.name = "flour"  # what a parser that split the compound would return
    apply_dictionary(line)
    assert (line.name, line.ingredient_name) == ("all-purpose flour", "all-purpose flour")


def test_dictionary_moves_prep_words_out_of_the_name():
    line = parse_line("1/2 cup melted butter")
    line.name = "melted butter"
    apply_dictionary(line)
    assert (line.name, line.preparation, line.ingredient_name) == ("butter", "melted", "butter")


def test_dictionary_keeps_products_that_look_like_prep():
    line = parse_line("1 (14 oz) can crushed tomatoes")
    apply_dictionary(line)
    assert line.ingredient_name == "crushed tomatoes"


def test_dictionary_maps_synonyms_but_keeps_the_written_name():
    line = parse_line("3 scallions, sliced")
    apply_dictionary(line)
    assert (line.name, line.ingredient_name) == ("scallions", "green onions")
