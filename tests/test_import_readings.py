"""Two readings of a recipe photo compared line by line (D-035). Lines written for these tests."""

from __future__ import annotations

from fractions import Fraction

import pytest

from thymble.importer import textnorm
from thymble.importer.readings import align, compare, split_device_text

# ------------------------------------------------------------------ normalization


@pytest.mark.parametrize(
    ("text", "values"),
    [
        ("1½ cups milk", [Fraction(3, 2)]),
        ("1 ½ cups milk", [Fraction(3, 2)]),
        ("1 1/2 cups milk", [Fraction(3, 2)]),
        ("1.5 cups milk", [Fraction(3, 2)]),
        ("1\u20442 tsp salt", [Fraction(1, 2)]),
        ("⅛ tsp clove", [Fraction(1, 8)]),
        ("2-3 tbsp honey", [Fraction(2), Fraction(3)]),
        ("0,5 l stock", [Fraction(1, 2)]),
        ("a pinch of salt", []),
    ],
)
def test_numbers_are_values(text, values):
    assert textnorm.numbers(text) == values


def test_parts_split_numbers_units_and_words():
    p = textnorm.parts("▢ 1½ Tbsps. Unsalted Butter, soft")
    assert p.numbers == (Fraction(3, 2),)
    assert p.units == ("tbsp",)
    assert p.words == ("unsalted", "butter", "soft")


def test_single_letter_units_only_after_a_number():
    assert textnorm.parts("2 T butter").units == ("tbsp",)
    assert textnorm.parts("2 t salt").units == ("tsp",)
    assert textnorm.parts("1 c sugar").units == ("cup",)
    # "a" / "l" as words are not units.
    assert textnorm.parts("add a little oil").units == ()


def test_comparable_ignores_case_punctuation_bullets_and_quotes():
    assert textnorm.comparable("• Mix WELL!") == textnorm.comparable("mix well")
    assert textnorm.comparable("“do.”") == textnorm.comparable('"do."')


# ------------------------------------------------------------------ one pair


@pytest.mark.parametrize(
    ("model", "device", "agreement"),
    [
        ("1 cup sugar", "1 cup sugar", "agree"),
        ("1½ cups milk", "1 1/2 Cups milk", "agree"),
        ("2 Tbsp butter", "2 tablespoons butter", "agree"),
        ("1/8 tsp salt", "1/2 tsp salt", "number"),
        ("2 tsp cinnamon", "2 tbsp cinnamon", "unit"),
        ("1 cup coarse salt", "1 cup course salt", "text"),
        ("3 eggs", "3 eggs, beaten", "text"),
        ("Bake at 350 for 1 hour", "Bake at 350 for 1 hour.", "agree"),
        ("Bake at 350 for 1 hour", "Bake at 325 for 1 hour", "number"),
    ],
)
def test_compare(model, device, agreement):
    assert compare(model, device)[0] == agreement


def test_compare_detail_names_the_numbers():
    agreement, detail = compare("1/8 tsp salt", "1/2 tsp salt")
    assert agreement == "number" and detail == "1/8 vs 1/2"
    assert compare("1½ cups", "2 cups")[1] == "1 1/2 vs 2"


# ------------------------------------------------------------------ alignment


def test_identical_readings_all_agree():
    lines = ["Toast", "Ingredients", "2 slices bread", "Method", "Toast it."]
    result = align(lines, list(lines))
    assert [p.agreement for p in result.pairs] == ["agree"] * 5
    assert [p.device_lines for p in result.pairs] == [[0], [1], [2], [3], [4]]
    assert result.unmatched_device == []


def test_a_line_only_the_model_has_does_not_shift_the_rest():
    model = ["1 cup sugar", "3 eggs", "1 tsp vanilla", "Stir."]
    device = ["1 cup sugar", "1 tsp vanilla", "Stir."]
    result = align(model, device)
    assert [p.agreement for p in result.pairs] == ["agree", "missing", "agree", "agree"]
    assert result.pairs[2].device_lines == [1]


def test_a_line_only_the_device_has_is_reported():
    model = ["1 cup sugar", "Stir."]
    device = ["1 cup sugar", "2 tbsp cocoa", "Stir."]
    result = align(model, device)
    assert [p.agreement for p in result.pairs] == ["agree", "agree"]
    assert result.unmatched_device == [1]


def test_device_line_broken_in_two_is_joined():
    model = ["Ingredients", "Beat the butter and sugar together until pale and fluffy.", "End"]
    device = ["Ingredients", "Beat the butter and sugar together", "until pale and fluffy.", "End"]
    result = align(model, device)
    assert result.pairs[1].device_lines == [1, 2]
    assert result.pairs[1].agreement == "agree"
    assert result.unmatched_device == []


def test_misread_number_pairs_up_and_is_flagged():
    model = ["Spice Cake", "1/8 tsp salt", "2 cups flour"]
    device = ["Spice Cake", "1/2 tsp salt", "2 cups flour"]
    pairs = align(model, device).pairs
    assert [p.agreement for p in pairs] == ["agree", "number", "agree"]
    assert pairs[1].device == "1/2 tsp salt"


def test_device_confidences_follow_their_lines():
    lines, scores = split_device_text("Toast\n\n2 slices bread\n", [0.9, 0.0, 0.4])
    assert lines == ["Toast", "2 slices bread"]
    assert scores == [0.9, 0.4]
    result = align(["Toast", "2 slices bread"], lines, scores)
    assert [p.device_confidence for p in result.pairs] == [0.9, 0.4]


def test_confidences_must_match_the_lines():
    with pytest.raises(ValueError, match="2 values for 3 lines"):
        split_device_text("a\nb\nc", [0.5, 0.5])


def test_readings_to_dict_keeps_both_readings():
    result = align(["1 cup sugar"], ["1 cup sugar"]).to_dict()
    assert result["model"] == ["1 cup sugar"] and result["device"] == ["1 cup sugar"]
    assert result["lines"][0]["agreement"] == "agree"
