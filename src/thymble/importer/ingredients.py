"""Ingredient line parsing: rules + ingredient-parser-nlp (architecture §5.5).

The parser model is loaded once per process (`load_parser()`, called at worker start-up) and
costs ~2 ms per line. A line is routed to the LLM (task T3) when its lowest field confidence is
below 0.90, it has no name, it contains "or"/"sub", or the recipe is not English: confidence
does not catch non-English errors (spike REPORT.md §2).

And/or convention: "and" / "&" / "and/or" -> separate items (`additional`); "or" / "sub" /
"(or X)" -> one item with substitutes (`alternatives`); "X / Y" names of one product -> one item.
"or as needed", "to taste" and "half-and-half" are not alternatives.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from functools import lru_cache
from typing import Any

from thymble.importer import rules

CONFIDENCE_THRESHOLD = 0.90

BRANDS = ["Diamond Crystal", "Morton", "King Arthur Unbleached", "King Arthur", "Bob's Red Mill"]
_AMOUNT = (
    r"[\d½¼¾⅓⅔⅛/.\s-]+\s*(?:cups?|tbsps?\.?|tsps?\.?|tablespoons?|teaspoons?|oz\.?|ounces?"
    r"|g|grams?|lbs?|pounds?|ml)?\.?"
)
_NOT_ALTERNATIVE = r"^(?:or )?(?:as needed|to taste|more|less|your preference|so)\b"
_OR_SUB = re.compile(r"\b(?:or|sub)\b(?!\s+(?:as needed|to taste|more|less|so)\b)", re.I)


@dataclass
class ParsedLine:
    raw_text: str
    text: str
    quantity: float | None = None
    quantity_max: float | None = None
    unit: str | None = None
    name: str | None = None
    additional: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    preparation: str | None = None
    note: str | None = None
    optional: bool = False
    confidence: float = 0.0
    flags: list[str] = field(default_factory=list)
    ditto: bool = False
    group: str | None = None
    # Filled by later stages.
    source: str = "rules"  # rules | llm
    review_reasons: list[str] = field(default_factory=list)
    ingredient_id: str | None = None
    ingredient_name: str | None = None

    @property
    def route_to_llm(self) -> bool:
        return bool(self.flags)

    @property
    def needs_review(self) -> bool:
        return bool(self.review_reasons)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["needs_review"] = self.needs_review
        return data


@lru_cache(maxsize=1)
def load_parser() -> Any:
    """Import and warm up ingredient-parser-nlp (~2 s); later calls are free."""
    from ingredient_parser import parse_ingredient

    parse_ingredient("1 cup flour")
    return parse_ingredient


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Fraction):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_amount(parsed: Any) -> tuple[float | None, float | None, str | None]:
    """First amount -> (quantity, quantity_max, unit). Composite amounts ("1 lb 2 oz") are
    summed into the first unit."""
    for amount in parsed.amount or []:
        if hasattr(amount, "amounts"):
            try:
                parts = amount.amounts
                total = parts[0].quantity * parts[0].unit
                for part in parts[1:]:
                    total = total + part.quantity * part.unit
                return float(total.to(parts[0].unit).magnitude), None, str(parts[0].unit)
            except Exception:
                return None, None, None
        quantity, quantity_max = _num(amount.quantity), _num(amount.quantity_max)
        if quantity is None:
            return None, None, None
        unit = str(amount.unit) if amount.unit not in (None, "") else None
        if quantity_max is not None and quantity_max == quantity:
            quantity_max = None
        return quantity, quantity_max, unit
    return None, None, None


def _confidence(parsed: Any) -> float:
    scores = [n.confidence for n in parsed.name or []]
    scores += [a.confidence for a in parsed.amount or []]
    scores += [
        f.confidence
        for f in (parsed.size, parsed.preparation, parsed.comment, parsed.purpose)
        if f is not None
    ]
    return min(scores) if scores else 0.0


def _strip_brand(name: str) -> str:
    for brand in BRANDS:
        if name.lower().startswith(brand.lower() + " "):
            return name[len(brand) + 1 :]
    return name


def _from_ditto(text: str, lang: str, prev: ParsedLine, group: str | None) -> ParsedLine | None:
    kind, arg = rules.ditto_kind(text)
    if kind is None:
        return None
    out = ParsedLine(**{**asdict(prev), "raw_text": text, "text": text, "group": group})
    out.additional, out.alternatives = list(prev.additional), list(prev.alternatives)
    out.ditto, out.confidence, out.flags, out.review_reasons = True, 1.0, [], []
    out.source, out.ingredient_id, out.ingredient_name = "rules", None, None
    if kind == "qty":
        amount = parse_line(f"{arg} x", lang)
        out.quantity, out.quantity_max = amount.quantity, amount.quantity_max
    elif kind == "qty_unit":
        amount = parse_line(f"{arg} x", lang)
        out.quantity, out.quantity_max, out.unit = amount.quantity, amount.quantity_max, amount.unit
    elif kind == "name":
        item = parse_line(arg or "", lang)
        out.name, out.additional, out.alternatives = item.name, item.additional, item.alternatives
        out.preparation, out.note = item.preparation, item.note
    return out


def parse_line(
    text: str, lang: str = "en", prev: ParsedLine | None = None, group: str | None = None
) -> ParsedLine:
    """Rules pipeline for one ingredient line. `prev` is the parsed line before it (ditto)."""
    if prev is not None:
        ditto = _from_ditto(text, lang, prev, group)
        if ditto is not None:
            return ditto
    s = rules.clean_line(text, lang)
    s, info = rules.lang_normalize(s, lang)
    s = rules.word_numbers(s)
    s = rules.plus_minus(s)
    s = re.sub(r"\s+&\s+", " and ", s)
    each = bool(re.search(r"\beach\b", s, re.I))
    s = re.sub(r"\bEACH\b", "", s).strip()
    if not s:
        return ParsedLine(raw_text=text, text=s, flags=["no name"], group=group)
    parsed = load_parser()(s)
    quantity, quantity_max, unit = _first_amount(parsed)
    names = [n.text for n in parsed.name or []]
    prep = parsed.preparation.text if parsed.preparation else None
    note = parsed.comment.text if parsed.comment else None
    low = s.lower()

    # Brand + same-product alternative: "Diamond Crystal or 3/4 tsp Morton kosher salt".
    brand_alt = False
    if names and names[0] in BRANDS and note and re.search(r"\b(kosher salt|salt)\b", note):
        names = [re.search(r"(kosher salt|salt)", note).group(1)]  # type: ignore[union-attr]
        brand_alt = True
    names = [_strip_brand(n) for n in names]

    alternatives: list[str] = []
    additional: list[str] = []
    # Parenthetical alternative inside the name: "oyster sauce (sub hoisin)".
    if names:
        m = re.match(r"^(.*?)\s*\((?:sub|or)\s+([^()]+?)\)?\s*$", names[0], flags=re.I)
        if m:
            names[0] = m.group(1)
            alternatives.append(m.group(2).strip())
    # Alternative in the comment: "(or other cooking oil)", "(or water; for steaming)".
    if note and not brand_alt:
        m = re.match(
            rf"^\(?\s*(?:or|sub)\s+(?:{_AMOUNT}\s+)?(?:other\s+)?([^\d(),;][^(),;]*?)\s*(?:[(),;]|$)",
            note,
            flags=re.I,
        )
        if m and not re.match(_NOT_ALTERNATIVE, m.group(0).strip("( "), flags=re.I):
            alternatives.append(m.group(1).strip())
    # Alias in parentheses: "green onions (scallions)" -> name + note.
    if names:
        m = re.match(r"^(.+?)\s*\(([^()]+)\)$", names[0])
        if m:
            names[0] = m.group(1)
            note = "; ".join(x for x in (note, m.group(2)) if x)
    # Comma lists inside one name; "dried thyme and oregano, 1 tsp each".
    split: list[str] = []
    for name in names:
        if lang == "de" and split and re.fullmatch(r"[a-zäöüß]+", name):
            continue  # inverted adjective: "Paprikapulver, rosenscharfes"
        parts = re.split(r",\s+", name) if lang == "en" and "," in name else [name]
        if each:
            parts = [y for x in parts for y in re.split(r"\s+and\s+", x)]
        split += [x.strip() for x in parts if x.strip()]
    names = split
    # Conjunctions the English parser does not split.
    if lang != "en" and names:
        conj_and = {"de": "und", "fr": "et", "it": "e", "es": "y"}.get(lang)
        conj_or = {"de": "oder", "fr": "ou", "it": "o", "es": "o"}.get(lang)
        for conj, bucket in ((conj_and, additional), (conj_or, alternatives)):
            if conj and re.search(rf"\s{conj}\s", names[0]):
                first, *rest = re.split(rf"\s+{conj}\s+", names[0])
                names[0] = first
                bucket += rest
    # A unit word left in front of the name: "cloves aglio".
    if names and unit is None:
        m = re.match(r"^(cloves?|sprigs?|bunch(?:es)?|pinch)\s+(.+)$", names[0], flags=re.I)
        if m:
            unit, names[0] = m.group(1), m.group(2)
    # Roles of the parser's extra names.
    extra = names[1:]
    if extra:
        if "/" in s and not re.search(r"\b(or|and)\b", low):
            extra = []  # "cornflour / cornstarch": one product, two names
        elif re.search(r"\band/or\b", low):
            additional += extra
        elif _OR_SUB.search(low):
            alternatives += extra
        else:
            additional += extra
    # "packed" is a measuring instruction -> preparation.
    m = re.search(r"\(?\b((?:tightly |firmly |lightly )?packed)\b\)?", low)
    if m and not (prep and "packed" in prep.lower()):
        prep = m.group(1) if not prep else f"{m.group(1)}, {prep}"
        if note:
            note = (
                re.sub(r"\(?\b(?:tightly |firmly |lightly )?packed\b\)?", "", note, flags=re.I)
                .strip(" ,")
                .strip()
                or None
            )
    if info.get("prep"):
        extra_prep = str(info["prep"])
        prep = extra_prep if not prep or prep in extra_prep else prep
    alternatives = [re.sub(rf"^{_AMOUNT}\s+", "", a).strip() for a in alternatives]
    name = names[0] if names else None
    if name:
        name = re.sub(r"\s*\*+$", "", name).strip(" ,")
    notes = [
        x
        for x in (note, "each" if each else None, "to taste" if info.get("to_taste") else None)
        if x
    ]
    confidence = _confidence(parsed)
    flags = []
    if not name:
        flags.append("no name")
    if confidence < CONFIDENCE_THRESHOLD:
        flags.append("low confidence")
    if _OR_SUB.search(low):
        flags.append("or/sub")
    if lang != "en":
        flags.append("non-English")
    return ParsedLine(
        raw_text=text,
        text=s,
        quantity=quantity,
        quantity_max=quantity_max,
        unit=rules.norm_unit(unit),
        name=name,
        additional=[a for a in additional if a],
        alternatives=[a for a in alternatives if a],
        preparation=prep,
        note="; ".join(notes) or None,
        optional=bool(re.search(r"\boptional\b", low)),
        confidence=round(confidence, 4),
        flags=flags,
        group=group,
    )


def parse_lines(lines: list[tuple[str, str | None]], lang: str = "en") -> list[ParsedLine]:
    """Parse (text, group) pairs in order, so ditto lines can see the line before them."""
    out: list[ParsedLine] = []
    prev: ParsedLine | None = None
    for text, group in lines:
        line = parse_line(text, lang, prev, group)
        out.append(line)
        prev = line
    return out
