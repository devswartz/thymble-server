"""Confidence for every imported item (D-034, D-035; architecture §5.8).

Every ingredient line, step and section (title, yield, times, groups, completeness) gets a
level (high / check / low) and plain-language reasons. Levels come only from evidence the
pipeline gathered: parser field scores, unreadable or cut-off words, where the LLM stepped
in, rules that sometimes misfire, readers that disagree, implausible amounts, names the
catalog didn't know, steps that look like headings, redirects and language. The model's
opinion of itself is never an input. Thresholds and reason texts: `thresholds.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from thymble.importer import rules, textnorm
from thymble.importer.readings import AGREE, MISSING, NUMBER, TEXT, UNIT, Readings, similarity
from thymble.importer.thresholds import (
    DEVICE_OCR_CHECK_BELOW,
    GRAMS_PER_UNIT,
    INGREDIENT_LIMITS,
    ITEM_MATCH_MIN_SIMILARITY,
    MAX_QUANTITY_BY_UNIT,
    MAX_TIME_MINUTES,
    PARSER_CHECK_BELOW,
    PARSER_LOW_BELOW,
    REASONS,
    SEVERITY,
    STEP_HEADING_MAX_WORDS,
    TSP_PER_UNIT,
    Level,
)

if TYPE_CHECKING:
    from thymble.importer.extract import RecipeDraft
    from thymble.importer.ingredients import ParsedLine

SECTIONS = ("title", "yield", "times", "groups", "completeness")
UNREADABLE = re.compile(r"\[\s*\?\s*\]")
CUT_OFF = re.compile(r"\[\s*cut\s*\]", re.I)
_CANONICAL_UNITS = frozenset(rules.UNIT_ALIASES.values())


@dataclass
class Assessment:
    codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def add(self, code: str, detail: object = None) -> None:
        if code in self.codes:
            return
        _, text = REASONS[code]
        self.codes.append(code)
        self.reasons.append(text.format(detail=detail) if "{detail}" in text else text)

    @property
    def level(self) -> Level:
        levels = [REASONS[c][0] for c in self.codes]
        return max(levels, key=SEVERITY.__getitem__, default=Level.HIGH)

    def to_dict(self) -> dict[str, Any]:
        return {"confidence": str(self.level), "reasons": self.reasons, "reason_codes": self.codes}


def worst(levels: list[Level | str]) -> Level:
    return max((Level(x) for x in levels), key=SEVERITY.__getitem__, default=Level.HIGH)


# ----------------------------------------------------------------------------- plausibility


def implausible_amount(
    quantity: float | None, quantity_max: float | None, unit: str | None, name: str | None
) -> str | None:
    """A short description of what looks wrong ("15 cup of salt"), or None if plausible.

    Table-driven (`thresholds.MAX_QUANTITY_BY_UNIT`, `INGREDIENT_LIMITS`): a general cap
    per unit, then tighter caps for ingredients that are used by the spoonful."""
    if quantity is None:
        return None
    amount = _amount_text(quantity, quantity_max, unit)
    if quantity <= 0 or (quantity_max is not None and quantity_max < quantity):
        return amount
    top = quantity_max if quantity_max is not None else quantity
    unit = rules.norm_unit(unit)
    cap = MAX_QUANTITY_BY_UNIT.get(unit)
    if cap is not None and top > cap:
        return f"{amount} of {name}" if name else amount
    if name and unit:
        lowered = name.lower()
        for pattern, max_tsp, max_g, _label in INGREDIENT_LIMITS:
            if not re.search(rf"\b(?:{pattern})\b", lowered):
                continue
            if unit in TSP_PER_UNIT and top * TSP_PER_UNIT[unit] > max_tsp:
                return f"{amount} of {name}"
            if unit in GRAMS_PER_UNIT and top * GRAMS_PER_UNIT[unit] > max_g:
                return f"{amount} of {name}"
            break
    return None


def _amount_text(quantity: float, quantity_max: float | None, unit: str | None) -> str:
    def num(v: float) -> str:
        return str(int(v)) if float(v).is_integer() else f"{v:g}"

    text = num(quantity) + (f"-{num(quantity_max)}" if quantity_max is not None else "")
    return f"{text} {unit}" if unit else text


def unknown_unit(unit: str | None) -> bool:
    return bool(unit) and unit not in _CANONICAL_UNITS and unit not in MAX_QUANTITY_BY_UNIT


def looks_like_heading(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    words = t.split()
    if t.endswith(":"):
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters):
        return True
    return len(words) <= STEP_HEADING_MAX_WORDS and not re.search(r"[.!?)]$", t)


def mark_photo_text(a: Assessment, text: str | None) -> None:
    if not text:
        return
    if UNREADABLE.search(text):
        a.add("photo_unreadable")
    if CUT_OFF.search(text):
        a.add("photo_cut_off")


# ----------------------------------------------------------------------------- photos


class PhotoMatcher:
    """Maps structured items back to transcription lines and reports the two readings.

    `resolved` is the model's transcription after ditto resolution (one entry per literal
    line, same order), which is what the structuring pass saw; `literal` is the model's
    reading as transcribed, which is what the device reading is compared with."""

    def __init__(
        self, resolved: list[str], readings: Readings | None, literal: list[str] | None = None
    ) -> None:
        self.resolved = resolved
        self.literal = (
            literal if literal is not None and len(literal) == len(resolved) else resolved
        )
        self.keys = [textnorm.comparable(x) for x in resolved]
        self.readings = readings
        self.cursor = 0

    def find(self, text: str, ordered: bool = True) -> list[int]:
        key = textnorm.comparable(text)
        if not key:
            return []
        start = self.cursor if ordered else 0
        order = list(range(start, len(self.keys))) + list(range(0, start))
        for i in order:
            if self.keys[i] == key:
                if ordered:
                    self.cursor = i + 1
                return [i]
        # A step the rules joined from several lines.
        parts = [i for i in order if len(self.keys[i]) >= 4 and self.keys[i] in key]
        if parts:
            parts.sort()
            run = [parts[0]]
            for i in parts[1:]:
                if i == run[-1] + 1:
                    run.append(i)
            joined = " ".join(self.resolved[i] for i in run)
            if similarity(joined, text) >= ITEM_MATCH_MIN_SIMILARITY:
                if ordered:
                    self.cursor = run[-1] + 1
                return run
        best, best_i = 0.0, None
        for i in order:
            s = similarity(self.resolved[i], text)
            if s > best:
                best, best_i = s, i
        if best_i is not None and best >= ITEM_MATCH_MIN_SIMILARITY:
            if ordered:
                self.cursor = best_i + 1
            return [best_i]
        return []

    def assess(self, a: Assessment, text: str | None, ordered: bool = True) -> dict | None:
        """Add photo evidence for one item; returns the item's two readings for review."""
        if not text:
            return None
        mark_photo_text(a, text)
        lines = self.find(text, ordered)
        if not lines:
            a.add("photo_unmatched")
            return {"model": None, "device": None, "agreement": None}
        model = " / ".join(self.literal[i] for i in lines)
        for i in lines:
            mark_photo_text(a, self.literal[i])
            if rules.ditto_kind(self.literal[i])[0] is not None:
                a.add("ditto_copied")  # resolved by code between the two passes
        if self.readings is None:
            a.add("photo_single_reading")
            return {"model": model, "device": None, "agreement": None}
        pairs = [self.readings.pairs[i] for i in lines]
        for pair in pairs:
            if pair.agreement == NUMBER:
                a.add("readings_number", pair.detail)
            elif pair.agreement == UNIT:
                a.add("readings_unit", pair.detail)
            elif pair.agreement == TEXT:
                a.add("readings_text", pair.detail)
            elif pair.agreement == MISSING:
                a.add(
                    "readings_missing_number"
                    if textnorm.has_number(pair.model)
                    else "readings_missing"
                )
            if (
                pair.agreement == AGREE
                and pair.device_confidence is not None
                and pair.device_confidence < DEVICE_OCR_CHECK_BELOW
            ):
                a.add("device_ocr_unsure")
        order = [AGREE, TEXT, MISSING, UNIT, NUMBER]
        agreement = max((p.agreement for p in pairs), key=order.index)
        device = " / ".join(p.device for p in pairs if p.device) or None
        return {"model": model, "device": device, "agreement": agreement}


# ----------------------------------------------------------------------------- items


def assess_line(
    line: ParsedLine,
    *,
    disagreeing: set[str] | None = None,
    ai_source: str | None = None,
) -> Assessment:
    a = Assessment()
    if not line.name:
        a.add("no_name")
    if not line.ditto:
        if line.parse_score < PARSER_LOW_BELOW and line.source != "llm":
            a.add("parser_very_unsure")
        elif line.parse_score < PARSER_CHECK_BELOW:
            a.add("parser_unsure")
    if line.source == "llm":
        a.add("llm_parsed")
    for reason in line.review_reasons:
        detail = reason.split("; ", 1)[-1].rstrip(")") if "; " in reason else reason
        a.add("llm_failed", detail)
    if line.ditto:
        a.add("ditto_copied")
    if line.alternatives or "or/sub" in line.flags:
        a.add("or_split")
    if "non-English" in line.flags:
        a.add("non_english")
    bad = implausible_amount(line.quantity, line.quantity_max, line.unit, line.name)
    if bad:
        a.add("implausible_amount", bad)
    if unknown_unit(line.unit):
        a.add("unknown_unit", line.unit)
    if line.resolved_by == "llm":
        a.add("catalog_ai_match", line.ingredient_label or line.name)
    elif line.resolved_by == "new":
        a.add("catalog_new")
    if disagreeing and textnorm.comparable(line.raw_text) in disagreeing:
        a.add("sources_disagree")
    if ai_source:
        a.add("ai_extracted" if ai_source.startswith("llm:T1") else "ai_structured")
    mark_photo_text(a, line.raw_text)
    return a


def assess_step(
    text: str,
    *,
    disagreeing: set[str] | None = None,
    ai_source: str | None = None,
) -> Assessment:
    a = Assessment()
    if looks_like_heading(text):
        a.add("step_maybe_heading")
    if disagreeing:
        key = textnorm.comparable(text)
        if any(d and d in key for d in disagreeing):
            a.add("sources_disagree")
    if ai_source:
        a.add("ai_extracted" if ai_source.startswith("llm:T1") else "ai_structured")
    mark_photo_text(a, text)
    return a


def field_source(draft: RecipeDraft, name: str) -> str | None:
    if name in draft.field_sources:
        return draft.field_sources[name]
    return draft.extracted_by[0] if len(draft.extracted_by) == 1 else None


def _ai(draft: RecipeDraft, name: str) -> str | None:
    source = field_source(draft, name)
    return source if source and source.startswith("llm") else None


def _slug_words(url: str | None) -> set[str]:
    if not url:
        return set()
    path = urlsplit(url).path.lower()
    words = set(re.findall(r"[a-z]{3,}", path))
    return words - {"recipe", "recipes", "www", "html", "htm", "php", "amp", "print", "index"}


def different_page(requested: str | None, final: str | None) -> bool:
    if not requested or not final:
        return False
    a, b = urlsplit(requested), urlsplit(final)

    def host(name: str | None) -> str:
        return (name or "").lower().removeprefix("www.")

    return host(a.hostname) != host(b.hostname) or a.path.rstrip("/") != b.path.rstrip("/")


def redirect_reason(requested: str | None, final: str | None, title: str | None) -> str | None:
    """None, "redirected" or "redirect_title_mismatch" (no word of the link in the title)."""
    if not different_page(requested, final):
        return None
    wanted = {w.rstrip("s") for w in _slug_words(requested)}
    have = {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", (title or "").lower())}
    if wanted and not wanted & have:
        return "redirect_title_mismatch"
    return "redirected"


# ----------------------------------------------------------------------------- whole import


@dataclass
class Report:
    sections: dict[str, dict[str, Any]]
    review_state: str  # needs_review | reviewed
    counts: dict[str, int]


def assess_import(
    *,
    kind: str,
    draft: RecipeDraft,
    lines: list[ParsedLine],
    steps: list[dict[str, Any]],
    yield_info: dict[str, Any],
    requested_url: str | None,
    final_url: str | None,
    heading_groups: set[str],
    transcription: list[str] | None = None,
    resolved_transcription: list[str] | None = None,
    readings: Readings | None = None,
    notes: list[tuple[str, str | None]] | None = None,
) -> Report:
    """Score every line and step in place (`line.confidence`, `step["confidence"]` ...) and
    return the section scores and the recipe's review state."""
    photo = kind == "photo"
    disagree: dict[str, Any] = getattr(draft, "disagreements", {}) or {}
    matcher = (
        PhotoMatcher(resolved_transcription or transcription or [], readings, transcription)
        if photo
        else None
    )

    # Title first: it comes first on the card, so it anchors the matcher's cursor.
    title = Assessment()
    if not draft.title:
        title.add("missing_title")
    else:
        if "title" in disagree:
            title.add("sources_disagree")
        if _ai(draft, "title"):
            title.add("ai_extracted" if _ai(draft, "title") == "llm:T1" else "ai_structured")
        redirect = redirect_reason(requested_url, final_url, draft.title)
        if redirect:
            title.add(redirect)
    title_readings = matcher.assess(title, draft.title) if matcher and draft.title else None

    for line in lines:
        a = assess_line(
            line,
            disagreeing=set(disagree.get("ingredients", ())),
            ai_source=None if photo else _ai(draft, "ingredients"),
        )
        line.readings = matcher.assess(a, line.raw_text) if matcher else None
        _apply(line, a)

    for step in steps:
        a = assess_step(
            step["text"],
            disagreeing=set(disagree.get("steps", ())),
            ai_source=None if photo else _ai(draft, "steps"),
        )
        step["readings"] = matcher.assess(a, step["text"]) if matcher else None
        step.update(a.to_dict())

    yield_a = Assessment()
    if yield_info.get("text") is None:
        yield_a.add("missing_yield")
    elif yield_info.get("quantity") is None:
        yield_a.add("yield_unparsed", yield_info.get("text"))
    if "yield" in disagree:
        yield_a.add("sources_disagree")
    if _ai(draft, "yield_raw"):
        yield_a.add("ai_extracted" if _ai(draft, "yield_raw") == "llm:T1" else "ai_structured")
    yield_readings = (
        matcher.assess(yield_a, str(yield_info["text"]), ordered=False)
        if matcher and yield_info.get("text")
        else None
    )

    times = Assessment()
    prep, cook, total = draft.prep_time_minutes, draft.cook_time_minutes, draft.total_time_minutes
    for value in (prep, cook, total):
        if value is not None and value > MAX_TIME_MINUTES:
            times.add("time_implausible", f"{value} minutes")
    if prep and cook and total and prep + cook > total:
        times.add("times_inconsistent")
    if "times" in disagree:
        times.add("sources_disagree")
    time_fields = ("prep_time_minutes", "cook_time_minutes", "total_time_minutes")
    if any(v is not None for v in (prep, cook, total)) and (
        photo or any(_ai(draft, f) for f in time_fields)
    ):
        times.add("ai_structured")

    groups = Assessment()
    names = sorted({g for g in [line.group for line in lines] + [s["group"] for s in steps] if g})
    group_readings: dict[str, Any] = {}
    if heading_groups:
        groups.add("heading_from_steps")
    if names and not photo and (_ai(draft, "ingredients") or _ai(draft, "steps")):
        groups.add("ai_structured")
    if matcher:
        for name in names:
            group_readings[name] = matcher.assess(groups, name, ordered=False)

    complete = Assessment()
    if not steps:
        complete.add("missing_steps")
    for code, detail in notes or []:
        complete.add(code, detail)
    if readings is not None:
        dropped = [
            j
            for j in readings.unmatched_device
            if len(textnorm.comparable(readings.device[j])) >= 3
        ]
        if dropped:
            complete.add("photo_lines_dropped", len(dropped))

    sections = {
        "title": {**title.to_dict(), "readings": title_readings},
        "yield": {**yield_a.to_dict(), "readings": yield_readings},
        "times": times.to_dict(),
        "groups": {**groups.to_dict(), "names": names, "readings": group_readings or None},
        "completeness": complete.to_dict(),
    }
    levels = [line.confidence for line in lines] + [s["confidence"] for s in steps]
    levels += [s["confidence"] for s in sections.values()]
    needs = photo or worst(levels) != Level.HIGH
    counts = {str(level): levels.count(str(level)) for level in Level}
    return Report(sections, "needs_review" if needs else "reviewed", counts)


def _apply(line: ParsedLine, a: Assessment) -> None:
    line.confidence = str(a.level)
    line.reasons = a.reasons
    line.reason_codes = a.codes
