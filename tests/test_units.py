"""The one unit table (`thymble.units`), shared by the importer and the shopping list."""

from __future__ import annotations

from decimal import Decimal

import pytest

from thymble import units
from thymble.importer import rules
from thymble.shopping.text import parse_entry


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("fl oz", "fl_oz"),
        ("fl. oz.", "fl_oz"),
        ("fluid ounces", "fl_oz"),
        ("fluid_ounce", "fl_oz"),  # the ingredient parser's pint unit name
        ("FL_OZ", "fl_oz"),
        ("pint", "pt"),
        ("pints", "pt"),
        ("qt", "qt"),
        ("Quarts", "qt"),
        ("gallons", "gal"),
        ("piece", "ct"),
        ("each", "ct"),
        ("T", "tbsp"),
        ("t", "tsp"),
        ("Tbsps.", "tbsp"),
        ("c", "cup"),
        ("kilos", "kg"),
        ("centilitres", "cl"),
        ("tins", "can"),
        ("packets", "package"),
        ("bunches", "bunch"),
        ("boxes", "box"),
        ("inches", "inch"),
        ("Smidgens", "smidgen"),  # unknown: cleaned up, kept
        ("glass", "glass"),
        ("", None),
        ("  ", None),
        (None, None),
    ],
)
def test_normalize(raw, code):
    assert units.normalize(raw) == code


def test_every_code_and_alias_maps_into_the_table():
    assert set(units.ALIASES.values()) == set(units.UNITS)
    for code in units.UNITS:
        assert code == code.lower() and " " not in code
        assert units.normalize(code) == code


def test_importer_and_shopping_agree():
    """Same spelling, same code, whichever side reads it."""
    for spelling in ("fluid ounces", "pints", "quart", "pieces", "tablespoons", "lbs", "tin"):
        recipe_code = rules.norm_unit(spelling)
        assert recipe_code == units.normalize(spelling)
        assert units.is_known(recipe_code), spelling
    assert parse_entry("2 pints ice cream").unit == rules.norm_unit("pints") == "pt"
    assert parse_entry("1 quart milk").unit == "qt"


def test_compatible_and_convert():
    assert units.compatible("fl_oz", "cup") and units.compatible("pt", "qt")
    assert units.compatible("inch", "cm") and units.compatible("ct", "each")
    assert not units.compatible("cup", "g")  # no density
    assert not units.compatible("can", "jar") and units.compatible("cans", "can")
    assert not units.compatible("ct", None)
    assert units.convert(Decimal(2), "pt", "qt") == Decimal("1.0000")
    assert units.convert(Decimal(16), "fl_oz", "pt") == Decimal("1.0000")
    assert units.convert(Decimal(1), "dl", "ml") == Decimal("100.0000")
    assert units.convert(Decimal(1), "cup", "g") is None


def test_merge_follows_the_list_rule():
    q = Decimal
    assert units.mergeable(q(1), "cup", q(2), "tbsp")
    assert units.mergeable(None, None, q(2), "lb")
    assert units.mergeable(q(2), "lb", None, None)
    assert not units.mergeable(q(1), "cup", q(100), "g")
    assert units.merge(q(1), "cup", q(8), "tbsp") == (q("1.5000"), "cup")
    assert units.merge(None, None, q(2), "lbs") == (q(2), "lb")
    assert units.merge(q(2), "lb", None, None) == (q(2), "lb")


def test_format_quantity():
    assert units.format_quantity(Decimal("8"), "fl_oz") == "8 fl oz"
    assert units.format_quantity(Decimal("6"), "ct") == "6"
    assert units.format_quantity(Decimal("1.50"), "pints") == "1.5 pt"
    assert units.format_quantity(None, "cup") == ""
