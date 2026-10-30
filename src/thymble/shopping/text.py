"""Free-text list entries ("urgent 2 lb chicken thighs") and name matching keys.

A small rules-only stand-in for the ingredient-parsing pipeline (architecture §5 step 5),
good enough for voice entries from Alexa. Urgency keywords follow §6.4 / D-029:
"urgent", "asap", "out of", "need" at the start (optionally with a colon), "urgent"/"asap"
at the end. A store name at the start ("costco paper towels") or after "from"/"at" at the
end ("bread from aldi") is a store hint. Non-food items are found by whole words, so
"soapberries" is food and "dish soap" is not (D-033).

These rules are pinned by the shared test vectors in tests/vectors/ (D-033).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from thymble.units import normalize as normalize_unit

# Suffix Thymble adds to stop-gap mirrors (design decision: "<name> (small)").
STOPGAP_SUFFIX = " (small)"

_URGENT_PREFIX = re.compile(
    r"^(?:(?:an?\s+)?urgent(?:ly)?\s*:?|asap\s*:?|"
    r"(?:we(?:'re| are)\s+|i'm\s+|i am\s+)?(?:all\s+)?out\s+of|"
    r"(?:we\s+|i\s+)?(?:really\s+)?need(?:s|ed)?(?:\s+(?:more|some))?)\s+",
    re.I,
)
_URGENT_SUFFIX = re.compile(r"[\s,(-]+(?:urgent(?:ly)?|asap)\)?\s*$", re.I)
# Our own decorations, in case a mirror's text comes back as a "new" entry.
_OUR_SUFFIX = re.compile(r"\s*(?:\(small\)|[—-]+\s*on .+ order)\s*$", re.I)
_PACKAGING = r"packs?|bags?|boxes|box|cans?|bottles?|jars?|bunch(?:es)?|dozen|x"
_UNIT_WORDS = (
    r"lbs?|pounds?|oz|ounces?|kg|kilos?|g|grams?|ml|l|liters?|litres?|gal(?:lons?)?|"
    r"cups?|tbsps?|tsps?|tablespoons?|teaspoons?|qts?|quarts?|pints?|ct|count|pieces?|pcs?|"
    + _PACKAGING
)
_QTY = re.compile(
    rf"^(?P<q>\d+\s+\d+/\d+|\d+(?:[./]\d+)?)\s*(?P<u>(?:{_UNIT_WORDS})\b\.?)?\s+(?P<rest>.+)$",
    re.I,
)


# Chains recognised as store hints without any household setup; a household's own store
# names are added by the caller. Values are the canonical hint ("trader joe's").
KNOWN_STORES: dict[str, str] = {
    "costco": "costco",
    "aldi": "aldi",
    "walmart": "walmart",
    "target": "target",
    "kroger": "kroger",
    "trader joe's": "trader joe's",
    "trader joes": "trader joe's",
    "sam's club": "sam's club",
    "sams club": "sam's club",
    "whole foods": "whole foods",
    "safeway": "safeway",
    "publix": "publix",
    "lidl": "lidl",
    "meijer": "meijer",
    "wegmans": "wegmans",
}

# Whole words (an optional plural "s"/"es" is allowed) that make an item non-food.
NON_FOOD_WORDS = (
    "paper towel", "toilet paper", "tissue", "napkin", "soap", "detergent", "bleach",
    "trash bag", "garbage bag", "zip-top bag", "sandwich bag", "aluminum foil", "foil",
    "plastic wrap", "parchment paper", "coffee filter", "sponge", "battery", "batteries",
    "light bulb", "charger", "diaper", "wipe", "shampoo", "conditioner", "toothpaste",
    "toothbrush", "deodorant", "razor", "ibuprofen", "acetaminophen", "vitamin",
    "cat food", "dog food", "cat litter", "kitty litter", "dryer sheet", "fabric softener",
)  # fmt: skip
_NON_FOOD = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in NON_FOOD_WORDS) + r")(?:s|es)?\b", re.I
)


def is_food(name: str) -> bool:
    """False for household, pharmacy, pet and electronics items, matched by whole word."""
    return _NON_FOOD.search(name) is None


@dataclass(frozen=True)
class ParsedEntry:
    name: str
    quantity: Decimal | None
    unit: str | None
    urgent: bool
    # Lower-case store named in the entry ("costco"), or None.
    store_hint: str | None = None
    is_food: bool = True


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


def _store_names(stores: Iterable[str]) -> dict[str, str]:
    names = dict(KNOWN_STORES)
    for store in stores:
        key = " ".join(store.lower().split())
        if key:
            names.setdefault(key, key)
    return names


def _strip_store(t: str, stores: Iterable[str]) -> tuple[str, str | None]:
    # Longest names first so "sam's club" wins over a store called "sam's".
    for name, canonical in sorted(_store_names(stores).items(), key=lambda kv: -len(kv[0])):
        pattern = re.escape(name)
        m = re.match(rf"^{pattern}\s+(?=\S)", t, re.I)
        if m:
            return t[m.end() :], canonical
        m = re.search(rf"\s+(?:from|at)\s+{pattern}\s*$", t, re.I)
        if m:
            return t[: m.start()], canonical
    return t, None


# Right / left single quotation mark and modifier letter apostrophe.
_CURLY_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


def straight_quotes(text: str) -> str:
    """Curly apostrophes (iOS keyboards and dictation write "we\u2019re") read as straight."""
    return text.translate(_CURLY_APOSTROPHES)


def parse_entry(text: str, stores: Iterable[str] = ()) -> ParsedEntry:
    """Parse a spoken or typed list entry. `stores` adds the household's own store names
    to the chains recognised as store hints."""
    stores = [straight_quotes(s) for s in stores]
    t = " ".join(straight_quotes(text).strip().split())
    t = _OUR_SUFFIX.sub("", t)
    urgent = False
    store_hint = None
    while True:
        before = t
        stripped = _URGENT_SUFFIX.sub("", _URGENT_PREFIX.sub("", t, count=1)).strip()
        if stripped != t:
            urgent, t = True, stripped
        if store_hint is None:
            t, store_hint = _strip_store(t, stores)
            t = t.strip()
        if t == before:
            break
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
    name = t or text.strip()
    return ParsedEntry(
        name=name,
        quantity=quantity,
        unit=unit,
        urgent=urgent,
        store_hint=store_hint,
        is_food=is_food(name),
    )


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
