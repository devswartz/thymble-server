"""Canonical unit codes and quantity math, shared by the importer and the shopping list.

One table (`UNITS`) names every unit Thymble stores in `recipe_ingredient.unit`,
`shopping_item.unit`, `shopping_item_source.unit` and `item_store_pref.typical_unit`;
`ALIASES` maps every spelling we accept to one of those codes. `normalize()` is the only
way a unit string becomes a code, whether it comes from a recipe line, the ingredient
parser, an LLM answer, a voice entry or the REST api.

Conversions need no ingredient knowledge: volume <-> volume, mass <-> mass and
length <-> length. Volume <-> mass (density) is deliberately left out. Everything else
("clove", "can", "bunch") only adds up with exactly the same unit. `piece` is a bare
count ("each", "ct", "pieces"); it is left out of display text ("3 eggs", not
"3 piece eggs").

Codes are the importer's (and ThymbleKit's): lower-case ASCII, "fl oz" with a space,
"pt", "qt", "piece", "package". Underscores read as spaces. Unknown units pass
through `normalize()` cleaned up (lower-case, singular), so nothing a user wrote is lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Unit:
    code: str
    # "volume" (base ml), "mass" (base g), "length" (base cm), "count", or None when the
    # unit only adds up with itself.
    dimension: str | None = None
    factor: Decimal | None = None  # size in the dimension's base unit
    aliases: tuple[str, ...] = ()


_TABLE: tuple[Unit, ...] = (
    # volume (base: ml)
    Unit("ml", "volume", Decimal("1"), ("milliliter", "millilitre", "mls")),
    Unit("cl", "volume", Decimal("10"), ("centiliter", "centilitre")),
    Unit("dl", "volume", Decimal("100"), ("deciliter", "decilitre")),
    Unit("l", "volume", Decimal("1000"), ("liter", "litre", "lt")),
    Unit("tsp", "volume", Decimal("4.92892"), ("teaspoon", "tsps", "t")),
    Unit("tbsp", "volume", Decimal("14.7868"), ("tablespoon", "tbsps", "tbs", "tbl", "tbls")),
    Unit(
        "fl oz",
        "volume",
        Decimal("29.5735"),
        ("fl. oz", "fl.oz", "floz", "fluid ounce", "fluid oz"),
    ),
    Unit("cup", "volume", Decimal("236.588"), ("c", "cups")),
    Unit("pt", "volume", Decimal("473.176"), ("pint", "pts")),
    Unit("qt", "volume", Decimal("946.353"), ("quart", "qts")),
    Unit("gal", "volume", Decimal("3785.41"), ("gallon", "gals")),
    # mass (base: g)
    Unit("mg", "mass", Decimal("0.001"), ("milligram",)),
    Unit("g", "mass", Decimal("1"), ("gram", "gr", "gm")),
    Unit("kg", "mass", Decimal("1000"), ("kilogram", "kilo", "kgs")),
    Unit("oz", "mass", Decimal("28.3495"), ("ounce", "ozs")),
    Unit("lb", "mass", Decimal("453.592"), ("pound", "lbs")),
    # length (base: cm), e.g. "1 inch ginger"
    Unit("cm", "length", Decimal("1"), ("centimeter", "centimetre")),
    Unit("inch", "length", Decimal("2.54"), ("inches",)),
    # a bare count
    Unit("piece", "count", Decimal("1"), ("each", "count", "ct", "pc", "pcs")),
    # only add up with themselves
    Unit("can", aliases=("tin",)),
    Unit("clove"),
    Unit("stick"),
    Unit("sheet"),
    Unit("pinch", aliases=("pinches",)),
    Unit("dash", aliases=("dashes",)),
    Unit("bunch", aliases=("bunches",)),
    Unit("package", aliases=("pack", "packet", "pkg")),
    Unit("slice"),
    Unit("sprig"),
    Unit("drop"),
    Unit("jar"),
    Unit("handful"),
    Unit("head"),
    Unit("stalk"),
    Unit("bag"),
    Unit("bottle"),
    Unit("box", aliases=("boxes",)),
)

UNITS: dict[str, Unit] = {u.code: u for u in _TABLE}

# Every accepted spelling (lower-case) -> code. Codes map to themselves.
ALIASES: dict[str, str] = {u.code: u.code for u in _TABLE}
for _u in _TABLE:
    for _alias in _u.aliases:
        ALIASES[_alias] = _u.code

QUANTUM = Decimal("0.0001")  # matches Numeric(12, 4)


def _lookup(s: str) -> str | None:
    if s in ALIASES:
        return ALIASES[s]
    # Plurals of a known spelling: "cups", "pounds", "tablespoons", "fluid ounces".
    for cut in ("es", "s"):
        if s.endswith(cut) and len(s) > len(cut) + 1 and s[: -len(cut)] in ALIASES:
            return ALIASES[s[: -len(cut)]]
    return None


def normalize(unit: object) -> str | None:
    """Canonical code for a unit string (or a `pint` unit object from the ingredient parser).

    Capital `T` is tablespoon, small `t` teaspoon (old cookbooks). Unknown units come back
    lower-case and singular ("Smidgens" -> "smidgen").
    """
    if unit is None:
        return None
    s = str(unit).strip().rstrip(".").strip()
    if s == "T":
        return "tbsp"
    s = " ".join(s.lower().replace("_", " ").split())
    if not s:
        return None
    code = _lookup(s)
    if code is not None:
        return code
    if s.endswith("s") and len(s) > 3 and not s.endswith("ss"):
        return s[:-1]
    return s


def is_known(unit: object) -> bool:
    return normalize(unit) in UNITS


def compatible(a: str | None, b: str | None) -> bool:
    """True when quantities in `a` and `b` can be added together."""
    a, b = normalize(a), normalize(b)
    if a == b:
        return True
    ua, ub = UNITS.get(a or ""), UNITS.get(b or "")
    return bool(ua and ub and ua.dimension and ua.dimension == ub.dimension)


def convert(quantity: Decimal, from_unit: str | None, to_unit: str | None) -> Decimal | None:
    """Convert between compatible units; None when they aren't compatible."""
    a, b = normalize(from_unit), normalize(to_unit)
    if a == b:
        return quantity
    if not compatible(a, b):
        return None
    value = quantity * UNITS[a].factor / UNITS[b].factor
    return value.quantize(QUANTUM)


def mergeable(
    quantity: Decimal | None, unit: str | None, other_quantity: Decimal | None, other: str | None
) -> bool:
    """Can an amount of (other_quantity, other) be merged into (quantity, unit)?

    The shopping-list rule (§6.1): same ingredient merges when either side has no amount
    or the units are compatible.
    """
    return quantity is None or other_quantity is None or compatible(unit, other)


def merge(
    quantity: Decimal | None, unit: str | None, other_quantity: Decimal | None, other: str | None
) -> tuple[Decimal | None, str | None]:
    """Add (other_quantity, other) into (quantity, unit), keeping the first unit.

    Call only when `mergeable()`. An amount merged into "no amount" takes over.
    """
    if other_quantity is None:
        return quantity, unit
    if quantity is None:
        return other_quantity, normalize(other)
    extra = convert(other_quantity, other, unit)
    return (quantity + (extra or 0)).quantize(QUANTUM), unit


def format_quantity(quantity: Decimal | None, unit: str | None) -> str:
    """'2 lb', '1.5 cup', '3', '8 fl oz' — for display and mirror text."""
    if quantity is None:
        return ""
    q = quantity.quantize(QUANTUM).normalize()
    text = f"{q:f}"
    u = normalize(unit)
    if u and u != "piece":
        text += f" {u}"
    return text
