"""Two readings of a recipe photo, compared line by line (D-035).

Reading A is the vision model's literal transcription (pass a). Reading B is the device's
own OCR (Apple Vision on the iPhone), sent with the photo. Neither is trusted on its own:
the model's errors look plausible (1/8 -> 1/2) and it never admits it can't read a word.

`align()` pairs the lines of the two readings in order (a similarity-weighted alignment,
so a line one reader missed doesn't shift everything after it), joins a run of device lines
when the device broke one line in two, and classifies each pair:

  agree    same numbers (as values: "1½" == "1 1/2"), same units, same words
  number   a number differs                 -> low
  unit     a unit differs                   -> low
  text     only other words differ          -> check
  missing  only the model has this line     -> low if it has a number, else check
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any

from thymble.importer import textnorm
from thymble.importer.thresholds import (
    MAX_READING_LINES,
    NUMBER_TOLERANCE,
    READING_PAIR_MIN_SIMILARITY,
)

AGREE, NUMBER, UNIT, TEXT, MISSING = "agree", "number", "unit", "text", "missing"


@dataclass
class LinePair:
    model: str
    device: str | None
    agreement: str
    device_confidence: float | None = None
    device_lines: list[int] = field(default_factory=list)
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Readings:
    model: list[str]
    device: list[str]
    pairs: list[LinePair]  # one per model line, same order
    unmatched_device: list[int]  # device lines no model line accounts for

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "device": self.device,
            "lines": [p.to_dict() for p in self.pairs],
            "unmatched_device_lines": self.unmatched_device,
        }


def split_device_text(
    text: str, confidences: list[float] | None = None
) -> tuple[list[str], list[float | None]]:
    """Device OCR text -> non-empty lines with their confidences (same order)."""
    raw = (text or "").splitlines()
    if confidences is not None and len(confidences) != len(raw):
        raise ValueError(
            f"device_ocr_confidences has {len(confidences)} values for {len(raw)} lines"
        )
    lines: list[str] = []
    scores: list[float | None] = []
    for i, line in enumerate(raw):
        if line.strip():
            lines.append(line.strip())
            scores.append(confidences[i] if confidences is not None else None)
    return lines[:MAX_READING_LINES], scores[:MAX_READING_LINES]


def similarity(a: str, b: str) -> float:
    ca, cb = textnorm.comparable(a), textnorm.comparable(b)
    if not ca and not cb:
        return 1.0
    return SequenceMatcher(None, ca, cb, autojunk=False).ratio()


def compare(model: str, device: str) -> tuple[str, str | None]:
    """Classify one pair of lines; the detail says what differs, in plain words."""
    a, b = textnorm.parts(model), textnorm.parts(device)
    if not textnorm.same_numbers(a.numbers, b.numbers, NUMBER_TOLERANCE):
        return NUMBER, f"{_fmt_numbers(a.numbers)} vs {_fmt_numbers(b.numbers)}"
    if a.units != b.units:
        return UNIT, f"{_fmt(a.units)} vs {_fmt(b.units)}"
    if a.words != b.words:
        return TEXT, f'"{textnorm.clean(model)}" vs "{textnorm.clean(device)}"'
    return AGREE, None


def _fmt(items: tuple[str, ...]) -> str:
    return ", ".join(items) if items else "none"


def _fmt_numbers(values: tuple) -> str:
    out = []
    for v in values:
        if v.denominator == 1:
            out.append(str(v.numerator))
        elif v > 1:
            whole = v.numerator // v.denominator
            out.append(f"{whole} {v - whole}")
        else:
            out.append(str(v))
    return ", ".join(out) if out else "no number"


def _align(model: list[str], device: list[str]) -> list[int | None]:
    """For each model line, the index of its device line (or None): the order-preserving
    pairing with the highest total similarity, pairs below the threshold not allowed."""
    n, m = len(model), len(device)
    sim = [[similarity(a, b) for b in device] for a in model]
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(score[i - 1][j], score[i][j - 1])
            s = sim[i - 1][j - 1]
            if s >= READING_PAIR_MIN_SIMILARITY:
                best = max(best, score[i - 1][j - 1] + s)
            score[i][j] = best
    match: list[int | None] = [None] * n
    i, j = n, m
    while i > 0 and j > 0:
        s = sim[i - 1][j - 1]
        if s >= READING_PAIR_MIN_SIMILARITY and score[i][j] == score[i - 1][j - 1] + s:
            match[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif score[i][j] == score[i - 1][j]:
            i -= 1
        else:
            j -= 1
    return match


def align(
    model: list[str], device: list[str], device_confidences: list[float | None] | None = None
) -> Readings:
    model = [line for line in model][:MAX_READING_LINES]
    confidences = device_confidences or [None] * len(device)
    match = _align(model, device)
    groups: list[list[int]] = [[j] if j is not None else [] for j in match]
    used = {j for j in match if j is not None}

    # The device may break one line in two (or the model join two): give an unmatched run of
    # device lines to the model line next to it when the joined text fits better.
    for i, js in enumerate(groups):
        lo = max((g[-1] for g in groups[:i] if g), default=-1) + 1
        hi = min((g[0] for g in groups[i + 1 :] if g), default=len(device))
        free = [j for j in range(lo, hi) if j not in used]
        if not free:
            continue
        if js:
            # Extend with free device lines right after (or before) the matched one.
            best = similarity(model[i], " ".join(device[j] for j in js))
            after, before = _run_after(js[-1], free), _run_before(js[0], free)
            candidates = [after[:k] for k in range(1, len(after) + 1)]
            candidates += [before[-k:] for k in range(1, len(before) + 1)]
            chosen: list[int] = []
            for candidate in candidates:
                joined = sorted(js + candidate)
                s = similarity(model[i], " ".join(device[j] for j in joined))
                if s > best:
                    best, chosen = s, candidate
            if chosen:
                groups[i] = sorted(js + chosen)
                used.update(chosen)
        else:
            # An unmatched model line: try the free run between its neighbours' matches.
            for k in range(len(free), 0, -1):
                for start in range(0, len(free) - k + 1):
                    run = free[start : start + k]
                    if run != list(range(run[0], run[0] + k)):
                        continue
                    s = similarity(model[i], " ".join(device[j] for j in run))
                    if s >= READING_PAIR_MIN_SIMILARITY:
                        groups[i] = run
                        used.update(run)
                        break
                if groups[i]:
                    break

    pairs: list[LinePair] = []
    for i, js in enumerate(groups):
        if not js:
            pairs.append(LinePair(model=model[i], device=None, agreement=MISSING))
            continue
        text = " ".join(device[j] for j in js)
        agreement, detail = compare(model[i], text)
        scores = [confidences[j] for j in js if j < len(confidences) and confidences[j] is not None]
        pairs.append(
            LinePair(
                model=model[i],
                device=text,
                agreement=agreement,
                device_confidence=min(scores) if scores else None,
                device_lines=js,
                detail=detail,
            )
        )
    unmatched = [j for j in range(len(device)) if j not in used]
    return Readings(model=model, device=device, pairs=pairs, unmatched_device=unmatched)


def _run_after(j: int, free: list[int]) -> list[int]:
    run = []
    while j + 1 in free:
        j += 1
        run.append(j)
    return run


def _run_before(j: int, free: list[int]) -> list[int]:
    run = []
    while j - 1 in free:
        j -= 1
        run.insert(0, j)
    return run
