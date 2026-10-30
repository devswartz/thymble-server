"""Deterministic rules for the import pipeline (architecture §5; spike import-eval).

Pure text functions, no model and no LLM, so they are cheap to test and run everywhere:
  lines : clean_line (scraper debris), word numbers ("a dozen"), "2 cups minus 2 tbsp",
          unit aliases ("Tbsps" -> tbsp), ditto marks ("do.", "same as above"),
          light non-English normalisation (unit words, "de"/"di", name-first order)
  recipe: title clean-up, recipeYield parsing, ISO-8601 durations, section-heading-as-step
          detection, step timer extraction, language detection
"""

from __future__ import annotations

import re
import unicodedata
from fractions import Fraction

from thymble import units

# ---------------------------------------------------------------------------------- units

# Canonical unit codes and their spellings live in `thymble.units`, shared with the
# shopping list; the import pipeline uses the same table.
UNIT_ALIASES: dict[str, str] = units.ALIASES


def norm_unit(unit: object) -> str | None:
    """Map a unit string (or a pint unit) to a canonical code; unknown units pass through."""
    return units.normalize(unit)


# ---------------------------------------------------------------------------------- numbers

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "sixteen": 16, "twenty": 20,
}  # fmt: skip
_WORD_NUM_RE = r"^\s*(" + "|".join(WORD_NUMBERS) + r")\b"
UNICODE_FRACTIONS = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8"}


def word_numbers(s: str) -> str:
    """Word amounts to digits: "a dozen eggs" -> "12 eggs", "two cloves" -> "2 cloves",
    "2 15 oz cans" -> "2 (15 oz) cans"."""
    s = re.sub(r"^\s*half an? dozen\b", "6", s, flags=re.I)
    s = re.sub(r"^\s*(?:a|one) dozen\b", "12", s, flags=re.I)
    s = re.sub(r"^\s*(\d+) dozen\b", lambda m: str(int(m.group(1)) * 12), s, flags=re.I)
    s = re.sub(r"^\s*half (?:a|an)\b", "0.5", s, flags=re.I)
    s = re.sub(r"^\s*a couple(?: of)?\b", "2", s, flags=re.I)
    s = re.sub(r"^\s*(?:a|an)\s+(?=(?:pinch|handful|dash|sprig|bunch)\b)", "1 ", s, flags=re.I)
    if not re.match(r"^\s*an?\s+(few|little|bit)\b", s, re.I):
        s = re.sub(r"^\s*an?\s+(?=[a-z])", "1 ", s, flags=re.I)
    s = re.sub(_WORD_NUM_RE, lambda m: str(WORD_NUMBERS[m.group(1).lower()]), s, flags=re.I)
    s = re.sub(
        r"^(\d+)\s*x\s*(\d+\s*[a-z]+)\s+(tins?|cans?|jars?|packets?)\b",
        r"\1 (\2) \3",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"^(\d+)\s+(\d+(?:\.\d+)?)[ -]?(oz|ounce|g)\.?\s+(tins?|cans?|jars?)\b",
        r"\1 (\2 \3) \4",
        s,
        flags=re.I,
    )
    return s


_TO_TBSP = {"cup": 16.0, "tbsp": 1.0, "tsp": 1 / 3}


def _number(text: str) -> float:
    text = UNICODE_FRACTIONS.get(text, text)
    return sum(float(Fraction(part)) for part in text.split())


def plus_minus(s: str) -> str:
    """ "2 cups minus 2 tablespoons sugar" -> "1.875 cups sugar" (volume units only)."""
    q = r"(\d+(?:\s+\d/\d|\.\d+)?|\d/\d|[½¼¾⅓⅔])"
    u = r"(cups?|tablespoons?|tbsps?\.?|teaspoons?|tsps?\.?)"
    m = re.match(rf"^{q}\s*{u}\s+(plus|minus)\s+{q}\s*{u}\s+(.*)$", s, flags=re.I)
    if not m:
        return s
    u1, u2 = norm_unit(m.group(2)), norm_unit(m.group(5))
    if u1 not in _TO_TBSP or u2 not in _TO_TBSP:
        return s
    sign = 1 if m.group(3).lower() == "plus" else -1
    total = _number(m.group(1)) + sign * _number(m.group(4)) * _TO_TBSP[u2] / _TO_TBSP[u1]
    return f"{round(total, 4)} {m.group(2)} {m.group(6)}"


# ---------------------------------------------------------------------------------- cleaning


def clean_line(s: str, lang: str = "en") -> str:
    """Remove scraper debris seen in real pages: checkbox glyphs, "(Note 1)" markers, "(, ...",
    leaked "undefined", trailing toggle numbers, footnote stars, odd dashes, trailing periods."""
    s = unicodedata.normalize("NFC", s)
    s = s.replace("\u25a2", " ").replace("\u2610", " ")  # checkbox glyphs
    s = re.sub(r"\(\s*,\s*", "(", s)
    s = re.sub(r"\s*\(Note \d+[^)]*\)", "", s)
    s = re.sub(r"\bundefined\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if lang == "en":  # metric/US toggle artifact "... thighs) 2" (Italian lines end "(...) 6")
        s = re.sub(r"\)\s+\d+$", ")", s)
    s = re.sub(r"(?<=[a-z])\*+", "", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"(\d)\s*[\u2014\u2013]\s*(\d)", r"\1-\2", s)
    s = re.sub(r"(\d)\s+-\s+(\d)", r"\1-\2", s)
    s = re.sub(r"\b(ml|g|kg|cl|gr)\.(?=\s)", r"\1", s)
    if len(s.split()) > 1:
        s = re.sub(r"(?<![A-Z]\.)(?<!\bdo)\.\s*$", "", s)
    return s.strip()


# ---------------------------------------------------------------------------------- language

_LANG_UNITS = {
    "fr": [
        (r"cuill[eè]res? à soupe|c\. ?à ?s\.?", "tbsp"),
        (r"cuill[eè]res? à caf[ée]|c\. ?à ?c\.?", "tsp"),
    ],
    "it": [(r"cucchiai(?:o)?", "tbsp"), (r"cucchiain[io]", "tsp"), (r"spicchi(?:o)?", "cloves")],
    "de": [
        (r"EL", "tbsp"),
        (r"TL", "tsp"),
        (r"Zehen?(?:/n)?", "cloves"),
        (r"Bund", "bunch"),
        (r"Zweig(?:e|/e)?", "sprigs"),
        (r"Prise(?:n)?", "pinch"),
    ],
    "es": [
        (r"cucharadas?", "tbsp"),
        (r"cucharaditas?", "tsp"),
        (r"dientes?", "cloves"),
        (r"kilos?", "kg"),
    ],
}
_TO_TASTE = r"\bq\.\s?b\.?|\bn\.\s?B\.?|\bau goût\b|\bal gusto\b"
_LANG_PREP = {
    "fr": r"\b(fondue?s?|hachée?s?|râpée?s?|émincée?s?|ramolli|battus?)\b",
    "it": r"\b(tritat[oaie]|grattugiat[oaie]|sciolt[oaie]|tagliat[oaie] a dadini)\b",
    "es": r"\b(picad[oa]s?|rallad[oa]s?)\b",
    "de": r"\b(gehackt|gewürfelt|gerieben|geschnitten)\b",
}


def lang_normalize(s: str, lang: str) -> tuple[str, dict[str, object]]:
    """Light rewrite of non-English lines so the English parser can find amounts and units.
    Names stay in the original language (translation is LLM/catalog work)."""
    info: dict[str, object] = {}
    if lang == "en":
        return s, info
    if re.search(_TO_TASTE, s):
        info["to_taste"] = True
        s = re.sub(_TO_TASTE, " ", s).strip()
    s = re.sub(r"\((?:n|e|s)\)|/(?:n|e|s)\b", "", s)  # German plural markers: Zwiebel(n)
    m = re.search(r"\b(große?n?|groß|kleine?n?|mittelgroße?n?)\b", s)
    if lang == "de" and m:
        info["size"] = m.group(1)
        s = s.replace(m.group(1), " ")
    for pattern, unit in _LANG_UNITS.get(lang, []):
        s = re.sub(rf"(?<=\d)\s*\b(?:{pattern})(?=\s|$)", f" {unit}", s)
    # Name-first lines: "Spaghetti 320 g"
    m = re.match(r"^(?P<name>\D.*?)\s+(?P<q>\d+(?:[.,]\d+)?)\s*(?P<u>kg|g|ml|l|cl)?$", s)
    if m:
        s = f"{m['q']} {m['u'] or ''} {m['name']}"
    s = re.sub(r"^([\d.,/\s-]+(?:[a-zA-Z]+\s+)?)(?:de|d'|d\u2019|di)\s*", r"\1", s)
    prep_re = _LANG_PREP.get(lang)
    if prep_re:
        m = re.search(r"\s*\(([^()]*)\)\s*$", s)
        if m and re.search(prep_re, m.group(1)):
            info["prep"] = m.group(1).strip()
            s = s[: m.start()]
        m = re.search(prep_re, s)
        if m and "prep" not in info:
            info["prep"] = m.group(0)
            s = (s[: m.start()] + s[m.end() :]).strip(" ,")
    s = re.sub(r"\(\s*\)", "", s)
    return re.sub(r"\s+", " ", s).strip(), info


_STOPWORDS = {
    "en": {"the", "and", "with", "of", "to", "in", "a", "until", "for", "into", "or", "cup"},
    "de": {"und", "die", "der", "mit", "den", "das", "in", "zu", "bis", "von", "eine", "ein"},
    "fr": {"et", "le", "la", "les", "de", "des", "du", "avec", "dans", "une", "un", "pour"},
    "it": {"e", "il", "la", "di", "con", "in", "per", "le", "un", "una", "del", "della"},
    "es": {"y", "el", "la", "de", "con", "en", "los", "las", "un", "una", "para", "del"},
}


def detect_language(text: str, default: str = "en") -> str:
    """Tiny stop-word vote; good enough to route non-English recipes to the LLM (§5.5)."""
    words = re.findall(r"[a-zäöüßàâçéèêëîïôûùñ]+", text.lower())
    if len(words) < 8:
        return default
    scores = {lang: sum(w in stop for w in words) for lang, stop in _STOPWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 3 or scores[best] <= scores.get(default, 0):
        return default
    return best


def normalize_lang(value: object) -> str | None:
    """ "en-US" -> "en"."""
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*([a-zA-Z]{2})\b", value)
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------------- ditto

_DITTO_TOKEN = r"(?:do\.?|ditto|\"|\u201c|\u201d|\u3003|'')"
_QTY_CHARS = r"[\d/.\s½¼¾⅓⅔]"


def ditto_kind(text: str) -> tuple[str | None, str | None]:
    """Classify a ditto line. Returns (kind, argument):
    ("all", None) "do." / "same as above"; ("qty", "2") '2 "'; ("qty_unit", "2 cups") "2 cups do.";
    ("name", "nutmeg") '" nutmeg' (same amount, new item); (None, None) otherwise."""
    t = text.strip()
    if re.fullmatch(rf"(?:{_DITTO_TOKEN}\s*)+|same as above|ditto", t, flags=re.I):
        return "all", None
    m = re.fullmatch(rf"({_QTY_CHARS}+)\s*([a-zA-Z.]+)\s*(?:do\.?|ditto)", t, flags=re.I)
    if m:
        return "qty_unit", f"{m.group(1).strip()} {m.group(2)}"
    m = re.fullmatch(rf"({_QTY_CHARS}+?)\s*(?:{_DITTO_TOKEN}\s*)+", t)
    if m:
        return "qty", m.group(1).strip()
    m = re.fullmatch(r"(?:\"|\u201c|\u201d|\u3003)\s+(.+)", t)
    if m:
        return "name", m.group(1)
    return None, None


_AMOUNT_PREFIX = re.compile(
    r"^\s*(?P<qty>(?:\d+\s+\d/\d|\d+/\d|\d+(?:\.\d+)?|[½¼¾⅓⅔⅛])(?:\s*-\s*\d+(?:/\d)?)?)"
    r"\s*(?P<unit>[a-zA-Z]+\.?)?\s+(?P<rest>.*)$"
)


def split_amount(line: str) -> tuple[str, str, str]:
    """ "1 cup sugar" -> ("1", "cup", "sugar"); unit is "" when the word is not a unit."""
    m = _AMOUNT_PREFIX.match(line)
    if not m:
        return "", "", line.strip()
    unit = m.group("unit") or ""
    rest = m.group("rest")
    if unit and norm_unit(unit) not in set(UNIT_ALIASES.values()):
        rest, unit = f"{unit} {rest}", ""
    return m.group("qty"), unit, rest.strip()


def resolve_ditto_lines(lines: list[str]) -> list[str]:
    """Rewrite ditto lines as full lines by copying quantity + unit (or the item) from the
    previous line. Used between the two photo passes (D-019): deterministic, not prompted."""
    out: list[str] = []
    for line in lines:
        kind, arg = ditto_kind(line)
        prev = out[-1] if out else None
        if kind is None or prev is None:
            out.append(line)
            continue
        qty, unit, rest = split_amount(prev)
        if kind == "all":
            out.append(prev)
        elif kind == "qty":
            out.append(" ".join(x for x in (arg, unit, rest) if x))
        elif kind == "qty_unit":
            out.append(f"{arg} {rest}")
        else:  # "name": same amount, different item
            out.append(" ".join(x for x in (qty, unit, arg) if x))
    return out


# ---------------------------------------------------------------------------------- recipe


def clean_title(title: str | None, h1: str | None = None) -> str:
    """Drop SEO suffixes: "Pancakes Recipe by Someone", "Pancakes | Site", "Pancakes von X"."""
    t = re.sub(r"\s+", " ", title or "").strip()
    h = re.sub(r"\s+", " ", h1 or "").strip()
    if h and len(h) >= 4 and t.lower().startswith(h.lower()):
        return h
    t = re.sub(r"\s+Recipe by .+$", "", t)
    t = re.sub(r"\s+von \S+$", "", t)
    t = re.sub(r"\s+[:|]\s+.+$", "", t)
    return t


_YIELD_FILLER = (
    r"\b(makes|serves|servings?|cuts into|personnes?|portionen|portions?|people|yield|about)\b"
)


def parse_yield(raw: object) -> dict[str, object]:
    """Parse schema.org recipeYield (str, number or list) ourselves: libraries get
    ["48", "4 dozen cookies"] wrong. Returns {quantity, quantity_max, unit, text}."""
    values = raw if isinstance(raw, list) else [raw]
    values = [str(v).strip() for v in values if v not in (None, "")]
    empty = {"quantity": None, "quantity_max": None, "unit": None, "text": None}
    if not values:
        return empty
    with_noun = [
        v
        for v in values
        if re.search(r"[a-zA-Zàâçéèêëîïôûùüÿñæœ]{3,}", re.sub(_YIELD_FILLER, "", v, flags=re.I))
    ]
    text = with_noun[0] if with_noun else max(values, key=len)
    s = text.split(";")[0]
    s = re.sub(
        r"\b(" + "|".join(WORD_NUMBERS) + r")\b",
        lambda m: str(WORD_NUMBERS[m.group(1).lower()]),
        s,
        flags=re.I,
    )
    m = re.search(r"(\d+(?:\.\d+)?)\s*dozen\b", s, flags=re.I)
    if m:
        unit = re.sub(r"^.*dozen\s*", "", s, flags=re.I).strip() or None
        return {
            "quantity": float(m.group(1)) * 12,
            "quantity_max": None,
            "unit": unit,
            "text": text,
        }
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:(?:-|\u2013|to)\s*(\d+(?:\.\d+)?))?\s*"
        r"(?:\(?[\d.]+[- ]?inch\)?\s*)?(.*)$",
        s,
    )
    if not m:
        return {**empty, "text": text}
    unit = re.sub(r"[()]", "", m.group(3)).strip().lower() or None
    if unit:
        unit = re.split(r"\s*\(", unit)[0].strip()
        if re.fullmatch(r"(servings?|serving\(s\)|personnes?|portionen|portions?|people)", unit):
            unit = "servings"
    if unit is None and not re.search(r"\bmakes\b", text, re.I):
        unit = "servings"
    return {
        "quantity": float(m.group(1)),
        "quantity_max": float(m.group(2)) if m.group(2) else None,
        "unit": unit,
        "text": text,
    }


def parse_duration(value: object) -> int | None:
    """ISO-8601 duration ("PT1H30M", "P0DT0H25M") -> whole minutes; plain numbers = minutes."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value) if value > 0 else None
    m = re.fullmatch(
        r"\s*P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?"
        r"(?:(\d+(?:\.\d+)?)S)?)?\s*",
        str(value),
        flags=re.I,
    )
    if not m or not any(m.groups()):
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in m.groups())
    total = round(days * 1440 + hours * 60 + minutes + seconds / 60)
    return total or None


def split_steps(steps: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """Merge sentences broken across steps, then turn section headings that a page returned as
    steps ("For the sauce:", "Make the dough") into the group name of the steps after them."""
    merged: list[list] = []
    for text, group in steps:
        text = text.strip()
        if not text:
            continue
        if merged and merged[-1][1] == group:
            last = merged[-1][0]
            if last.endswith(",") or (text[:1].islower() and not re.search(r"[.!?:]$", last)):
                merged[-1][0] = f"{last} {text}"
                continue
        merged.append([text, group])
    out: list[tuple[str, str | None]] = []
    heading: str | None = None
    for i, (text, group) in enumerate(merged):
        words = len(text.split())
        is_last = i == len(merged) - 1
        if (text.endswith(":") and words <= 12) or (
            words <= 6 and not re.search(r"[.!?]$", text) and not is_last
        ):
            heading = text.rstrip(":").strip()
            continue
        out.append((text, group or heading))
    return out


_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "ten": 10, "un": 1, "una": 1, "une": 1, "ein": 1, "eine": 1,
}  # fmt: skip
_TIME_UNITS = [
    (r"seconds?|secs?|secondes?|secondi|Sekunden|segundos?", 1),
    (r"minutes?|mins?\.?|min\b|minuti|Minuten|minutos?", 60),
    (r"hours?|hrs?|ore|ora|Stunden?|horas?|heures?", 3600),
]
_NUM = (
    r"(\d+\s+\d/\d|\d+/\d|\d+(?:[.,]\d+)?|a|an|one|two|three|four|five|six|ten|un|una|une|ein|eine)"
)


def _to_float(text: str) -> float:
    text = text.strip().replace(",", ".")
    if text.lower() in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[text.lower()])
    if " " in text:
        whole, frac = text.split()
        return int(whole) + float(Fraction(frac))
    return float(Fraction(text)) if "/" in text else float(text)


def detect_timers(text: str) -> list[dict[str, object]]:
    """Durations in a step -> [{"label", "seconds", "seconds_max"?}] (architecture §5.7).
    "up to 10 minutes" / "within 5 minutes" are limits, not timers, and are skipped."""
    t = re.sub(r"\bhalf an hour\b", "30 minutes", text, flags=re.I)
    t = re.sub(r"\ban hour and a half\b", "90 minutes", t, flags=re.I)
    found: list[dict[str, object]] = []
    for pattern, multiplier in _TIME_UNITS:
        rx = (
            rf"(?<![\w/]){_NUM}(?:\s*(?:-|\u2013|\u2014|to|à|bis|o)\s*{_NUM})?"
            rf"[\s-]*(?:{pattern})(?![a-zA-Z])"
        )
        for m in re.finditer(rx, t, flags=re.I):
            before = t[max(0, m.start() - 14) : m.start()].lower()
            if re.search(r"(up to|within|no more than|jusqu'à)\s*$", before):
                continue
            timer: dict[str, object] = {
                "label": m.group(0).strip(),
                "seconds": round(_to_float(m.group(1)) * multiplier),
            }
            if m.group(2):
                timer["seconds_max"] = round(_to_float(m.group(2)) * multiplier)
            if timer["seconds"] and timer not in found:
                found.append(timer)
    return found
