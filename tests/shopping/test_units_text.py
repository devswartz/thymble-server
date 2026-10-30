from decimal import Decimal

import pytest

from thymble.shopping.text import name_key, parse_entry
from thymble.units import compatible, convert, format_quantity
from thymble.units import normalize as normalize_unit


@pytest.mark.parametrize(
    "text,name,qty,unit,urgent",
    [
        ("milk", "milk", None, None, False),
        ("Milk", "Milk", None, None, False),
        ("urgent toilet paper", "toilet paper", None, None, True),
        ("Urgent toilet paper", "toilet paper", None, None, True),
        ("out of coffee", "coffee", None, None, True),
        ("we're out of coffee", "coffee", None, None, True),
        ("need paper towels", "paper towels", None, None, True),
        ("we need more eggs", "eggs", None, None, True),
        ("toilet paper urgent", "toilet paper", None, None, True),
        ("2 lb chicken thighs", "chicken thighs", Decimal(2), "lb", False),
        ("urgent 2 lb chicken thighs", "chicken thighs", Decimal(2), "lb", True),
        ("1 1/2 cups flour", "flour", Decimal("1.5"), "cup", False),
        ("3 eggs", "eggs", Decimal(3), None, False),
        ("2 green onions", "green onions", Decimal(2), None, False),
        ("paper towels — on Walmart order", "paper towels", None, None, False),
        ("toilet paper (small)", "toilet paper", None, None, False),
        ("needles", "needles", None, None, False),  # 'need' only as a whole word
        ("outdoor bags", "outdoor bags", None, None, False),
    ],
)
def test_parse_entry(text, name, qty, unit, urgent):
    p = parse_entry(text)
    assert (p.name, p.quantity, p.unit, p.urgent) == (name, qty, unit, urgent)


def test_name_key_matches_plural_and_case():
    assert name_key("Eggs") == name_key("egg")
    assert name_key("Paper Towels") == name_key("paper towel")
    assert name_key("berries") == name_key("berry")
    assert name_key("glass") == "glass"


def test_units_normalize_and_convert():
    assert normalize_unit("Tablespoons") == "tbsp"
    assert normalize_unit("lbs") == "lb"
    assert normalize_unit("cloves") == "clove"
    assert compatible("cup", "tbsp") and compatible("lb", "g")
    assert not compatible("cup", "g")  # no density conversions
    assert not compatible("clove", "cup")
    assert compatible(None, None) and compatible("clove", "cloves")
    assert convert(Decimal(16), "tbsp", "cup") == Decimal("1.0000")
    assert convert(Decimal(1), "lb", "oz") == Decimal("16.0000")
    assert convert(Decimal(1), "cup", "g") is None


def test_format_quantity():
    assert format_quantity(Decimal("2.0000"), "lb") == "2 lb"
    assert format_quantity(Decimal("1.5"), "cups") == "1.5 cup"
    assert format_quantity(Decimal(3), None) == "3"
    assert format_quantity(None, "lb") == ""


def test_household_store_names_are_hints():
    from thymble.shopping.text import parse_entry

    p = parse_entry("eggs from Corner Market", ["Corner Market"])
    assert (p.name, p.store_hint) == ("eggs", "corner market")
    assert parse_entry("eggs from Corner Market").store_hint is None
