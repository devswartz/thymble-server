"""Units for merging shopping quantities (architecture §6.1).

Only conversions that need no ingredient knowledge: volume <-> volume and mass <-> mass.
Volume <-> mass (density) is deliberately left out. Anything else ("clove", "can") only
merges with exactly the same unit.
"""

from __future__ import annotations

from decimal import Decimal

# Canonical code -> (dimension, factor to the dimension's base unit: ml or g).
_UNITS: dict[str, tuple[str, Decimal]] = {
    "ml": ("volume", Decimal("1")),
    "l": ("volume", Decimal("1000")),
    "tsp": ("volume", Decimal("4.92892")),
    "tbsp": ("volume", Decimal("14.7868")),
    "fl_oz": ("volume", Decimal("29.5735")),
    "cup": ("volume", Decimal("236.588")),
    "pint": ("volume", Decimal("473.176")),
    "quart": ("volume", Decimal("946.353")),
    "gal": ("volume", Decimal("3785.41")),
    "mg": ("mass", Decimal("0.001")),
    "g": ("mass", Decimal("1")),
    "kg": ("mass", Decimal("1000")),
    "oz": ("mass", Decimal("28.3495")),
    "lb": ("mass", Decimal("453.592")),
}

_ALIASES: dict[str, str] = {
    "milliliter": "ml",
    "millilitre": "ml",
    "liter": "l",
    "litre": "l",
    "teaspoon": "tsp",
    "t": "tsp",
    "tablespoon": "tbsp",
    "tbs": "tbsp",
    "tbl": "tbsp",
    "fl oz": "fl_oz",
    "fluid ounce": "fl_oz",
    "floz": "fl_oz",
    "c": "cup",
    "pt": "pint",
    "qt": "quart",
    "gallon": "gal",
    "milligram": "mg",
    "gram": "g",
    "gr": "g",
    "kilogram": "kg",
    "kilo": "kg",
    "ounce": "oz",
    "pound": "lb",
    "lbs": "lb",
    "each": "ct",
    "count": "ct",
    "piece": "ct",
    "pc": "ct",
}

QUANTUM = Decimal("0.0001")  # matches Numeric(12, 4)


def normalize_unit(unit: str | None) -> str | None:
    """Canonical unit code, or the cleaned-up unit itself if it isn't a known one."""
    if unit is None:
        return None
    u = " ".join(unit.strip().lower().rstrip(".").split())
    if not u:
        return None
    if u in _UNITS or u == "ct":
        return u
    if u in _ALIASES:
        return _ALIASES[u]
    # Plurals: "cups", "pounds", "tablespoons", "boxes"
    for cut in ("es", "s"):
        if u.endswith(cut) and len(u) > len(cut) + 1:
            stem = u[: -len(cut)]
            if stem in _UNITS or stem == "ct":
                return stem
            if stem in _ALIASES:
                return _ALIASES[stem]
    if u.endswith("s") and len(u) > 3 and not u.endswith("ss"):
        return u[:-1]
    return u


def is_known_unit(unit: str | None) -> bool:
    u = normalize_unit(unit)
    return u in _UNITS or u in {"ct"}


def compatible(a: str | None, b: str | None) -> bool:
    """True when quantities in `a` and `b` can be added together."""
    a, b = normalize_unit(a), normalize_unit(b)
    if a == b:
        return True
    if a in _UNITS and b in _UNITS:
        return _UNITS[a][0] == _UNITS[b][0]
    return False


def convert(quantity: Decimal, from_unit: str | None, to_unit: str | None) -> Decimal | None:
    """Convert between compatible units; None when they aren't compatible."""
    a, b = normalize_unit(from_unit), normalize_unit(to_unit)
    if a == b:
        return quantity
    if not compatible(a, b):
        return None
    value = quantity * _UNITS[a][1] / _UNITS[b][1]
    return value.quantize(QUANTUM)


def format_quantity(quantity: Decimal | None, unit: str | None) -> str:
    """'2 lb', '1.5 cup', '3' — for mirror text."""
    if quantity is None:
        return ""
    q = quantity.quantize(QUANTUM).normalize()
    text = f"{q:f}"
    u = normalize_unit(unit)
    if u and u != "ct":
        text += f" {u.replace('_', ' ')}"
    return text
