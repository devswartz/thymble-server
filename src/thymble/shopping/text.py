"""Free-text list entries ("urgent 2 lb chicken thighs") and name matching keys.

A small rules-only stand-in for the ingredient-parsing pipeline (architecture §5 step 5),
good enough for voice entries from Alexa. Urgency keywords follow §6.4 / D-029:
"urgent", "out of", "need" at the start, "urgent"/"asap" at the end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from thymble.shopping.units import normalize_unit

# Suffix Thymble adds to stop-gap mirrors (design decision: "<name> (small)").
STOPGAP_SUFFIX = " (small)"

_URGENT_PREFIX = re.compile(
    r"^(?:(?:an?\s+)?urgent(?:ly)?|(?:we(?:'re| are)\s+|i'm\s+|i am\s+)?(?:all\s+)?out\s+of|"
    r"(?:we\s+|i\s+)?(?:really\s+)?need(?:s)?(?:\s+(?:more|some))?)\s+",
    re.I,
)
_URGENT_SUFFIX = re.compile(r"[\s,(-]+(?:urgent(?:ly)?|asap)\)?\s*$", re.I)
# Our own decorations, in case a mirror's text comes back as a "new" entry.
_OUR_SUFFIX = re.compile(r"\s*(?:\(small\)|[—-]+\s*on .+ order)\s*$", re.I)
_PACKAGING = r"packs?|bags?|boxes|box|cans?|bottles?|jars?|bunch(?:es)?|dozen|x"
_UNIT_WORDS = (
    r"lbs?|pounds?|oz|ounces?|kg|kilos?|g|grams?|ml|l|liters?|litres?|gal(?:lons?)?|"
    r"cups?|tbsps?|tsps?|tablespoons?|teaspoons?|qts?|quarts?|pints?|ct|count|" + _PACKAGING
)
_QTY = re.compile(
    rf"^(?P<q>\d+\s+\d+/\d+|\d+(?:[./]\d+)?)\s*(?P<u>(?:{_UNIT_WORDS})\b\.?)?\s+(?P<rest>.+)$",
    re.I,
)


@dataclass(frozen=True)
class ParsedEntry:
    name: str
    quantity: Decimal | None
    unit: str | None
    urgent: bool


def _number(text: str) -> Decimal | None:
    try:
        parts = text.split()
        if len(parts) == 2:  # "1 1/2"
            return Decimal(parts[0]) + _number(parts[1])
        if "/" in text:
            num, den = text.split("/")
            return Decimal(num) / Decimal(den)
        return Decimal(text)
    except (InvalidOperation, ZeroDivisionError, TypeError):
        return None


def parse_entry(text: str) -> ParsedEntry:
    t = " ".join(text.strip().split())
    t = _OUR_SUFFIX.sub("", t)
    urgent = False
    while True:
        new = _URGENT_PREFIX.sub("", t, count=1)
        new = _URGENT_SUFFIX.sub("", new)
        if new == t:
            break
        urgent, t = True, new.strip()
    quantity = unit = None
    m = _QTY.match(t)
    if m:
        q = _number(m.group("q"))
        if q is not None:
            quantity, t = q, m.group("rest")
            raw_unit = (m.group("u") or "").rstrip(".")
            unit = normalize_unit(raw_unit) if raw_unit else None
            if unit == "x":
                unit = None
    return ParsedEntry(name=t or text.strip(), quantity=quantity, unit=unit, urgent=urgent)


def normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def name_key(name: str) -> str:
    """Matching key: lower-case, whitespace collapsed, naive singular of the last word."""
    words = normalize_name(name).split()
    if words:
        last = words[-1]
        if len(last) > 4 and last.endswith("ies"):
            words[-1] = last[:-3] + "y"
        elif len(last) > 4 and last.endswith(("ches", "shes", "xes", "oes")):
            words[-1] = last[:-2]
        elif len(last) > 3 and last.endswith("s") and not last.endswith(("ss", "us")):
            words[-1] = last[:-1]
    return " ".join(words)


def name_variants(name: str) -> set[str]:
    """Lower-case spellings that may share `name_key(name)` (for SQL lookups)."""
    norm, key = normalize_name(name), name_key(name)
    return {norm, key, key + "s", key + "es", key[:-1] + "ies" if key.endswith("y") else key}
