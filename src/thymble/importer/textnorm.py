"""Text normalization for comparing two versions of the same line.

Used to compare the two photo readings (D-035) and the page's two structured readers
(schema.org vs site scraper). Numbers are compared as values, so "1½", "1 1/2", "1 ½" and
"1.5" are the same, and units through `rules.norm_unit`, so "Tbsp" and "tablespoon" are too.
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction

from thymble.importer import rules

UNICODE_FRACTIONS = {
    "½": "1/2",
    "⅓": "1/3",
    "⅔": "2/3",
    "¼": "1/4",
    "¾": "3/4",
    "⅕": "1/5",
    "⅖": "2/5",
    "⅗": "3/5",
    "⅘": "4/5",
    "⅙": "1/6",
    "⅚": "5/6",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}
_QUOTES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2033": '"'}
_DASHES = {"\u2013": "-", "\u2014": "-", "\u2212": "-"}
_NUMBER = re.compile(r"\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?")
# Bullets and checkbox debris some pages and OCR put in front of a line.
_LEADING_DEBRIS = re.compile(r"^[\s\-*•·▢□☐◦‣>]+")


def expand_fractions(text: str) -> str:
    """ "1½" -> "1 1/2", "½" -> "1/2", fraction slash -> "/"."""
    text = text.replace("\u2044", "/")
    for char, plain in UNICODE_FRACTIONS.items():
        text = re.sub(rf"(\d)\s*{char}", rf"\1 {plain}", text)
        text = text.replace(char, plain)
    return text


def clean(text: str) -> str:
    """Original case, unified fractions, quotes, dashes and whitespace; no leading bullets."""
    text = unicodedata.normalize("NFC", text or "")
    for table in (_QUOTES, _DASHES):
        for char, plain in table.items():
            text = text.replace(char, plain)
    text = expand_fractions(text)
    text = _LEADING_DEBRIS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def comparable(text: str) -> str:
    """Lower-case text with punctuation dropped, for similarity and equality checks."""
    s = clean(text).lower()
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    s = re.sub(r"[^\w\s/.'\"-]", " ", s)
    s = re.sub(r"(?<!\d)[.,;:!?](?!\d)|[.,;:!?]$", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def to_value(token: str) -> Fraction:
    token = token.replace(",", ".").strip()
    if " " in token:
        whole, frac = token.split()
        return int(whole) + Fraction(frac)
    return Fraction(token)


def numbers(text: str) -> list[Fraction]:
    """Every number in the line as a value, in order ("1 1/2-2 cups" -> [3/2, 2])."""
    out = []
    for m in _NUMBER.finditer(clean(text)):
        try:
            out.append(to_value(m.group(0)))
        except (ValueError, ZeroDivisionError):
            continue
    return out


_CANONICAL_UNITS = frozenset(rules.UNIT_ALIASES.values())
_TOKEN = re.compile(
    r"(?P<num>\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?)"
    r"|(?P<word>[^\W\d_]+(?:['.][^\W\d_]+)*\.?)"
)


def _unit_of(token: str) -> str | None:
    bare = token.rstrip(".")
    if bare.lower() not in rules.UNIT_ALIASES and bare not in ("T", "t"):
        return None
    unit = rules.norm_unit(token)
    return unit if unit in _CANONICAL_UNITS else None


@dataclass(frozen=True)
class LineParts:
    numbers: tuple[Fraction, ...]
    units: tuple[str, ...]
    words: tuple[str, ...]


def parts(text: str) -> LineParts:
    """Split a line into number values, canonical units and the remaining words."""
    nums: list[Fraction] = []
    units: list[str] = []
    words: list[str] = []
    after_number = False
    for m in _TOKEN.finditer(clean(text)):
        if m.group("num"):
            with contextlib.suppress(ValueError, ZeroDivisionError):
                nums.append(to_value(m.group("num")))
            after_number = True
            continue
        token = m.group("word")
        unit = _unit_of(token)
        # Single letters are units only right after a number ("2 T butter", "1 c sugar").
        if unit is not None and (len(token.rstrip(".")) > 1 or after_number):
            units.append(unit)
        else:
            words.append(token.lower().rstrip("."))
        after_number = False
    return LineParts(tuple(nums), tuple(units), tuple(words))


def same_numbers(a: tuple[Fraction, ...], b: tuple[Fraction, ...], tolerance: float) -> bool:
    return len(a) == len(b) and all(
        abs(float(x - y)) < tolerance for x, y in zip(a, b, strict=True)
    )


def has_number(text: str) -> bool:
    return bool(numbers(text)) or any(ch in UNICODE_FRACTIONS for ch in text)
