"""Shared line-rule vectors (D-033): the server rules are the reference implementation.

tests/vectors/ingredient_lines.json is also run by ThymbleKit (Swift). Format and comparison
rules: tests/vectors/README.md. A failure here is a server bug or a wrong vector, never
something to paper over in this file.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from thymble.importer.ingredients import parse_lines
from thymble.shopping.text import parse_entry

VECTORS = Path(__file__).parent / "vectors" / "ingredient_lines.json"
DOC = json.loads(VECTORS.read_text(encoding="utf-8"))
CASES = DOC["cases"]
QUANTITY_TOLERANCE = 0.001


def _text(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def _same(field: str, actual: Any, expected: Any) -> bool:
    if field in ("quantity", "quantity_max"):
        if actual is None or expected is None:
            return actual is expected
        return abs(float(actual) - float(expected)) <= QUANTITY_TOLERANCE
    if isinstance(expected, list):
        return [_text(a) for a in actual or []] == [_text(e) for e in expected]
    return _text(actual) == _text(expected)


def _recipe_line(case: dict[str, Any]) -> dict[str, Any]:
    lines = [(text, None) for text in case.get("previous", [])] + [(case["input"], None)]
    parsed = parse_lines(lines)[-1]
    return {
        "quantity": parsed.quantity,
        "quantity_max": parsed.quantity_max,
        "unit": parsed.unit,
        "name": parsed.name,
        "additional": parsed.additional,
        "alternatives": parsed.alternatives,
        "preparation": parsed.preparation,
        "optional": parsed.optional,
        "ditto": parsed.ditto,
    }


def _voice_item(case: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_entry(case["input"])
    return {
        "name": parsed.name,
        "quantity": parsed.quantity if parsed.quantity is None else Decimal(parsed.quantity),
        "unit": parsed.unit,
        "urgent": parsed.urgent,
        "store": parsed.store_hint,
        "is_food": parsed.is_food,
    }


RUNNERS = {"recipe_line": _recipe_line, "voice_item": _voice_item}


def test_file_shape():
    assert DOC["version"] == 1
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    assert len(CASES) >= 60
    for case in CASES:
        assert case["kind"] in RUNNERS, case["id"]
        assert case["expect"], case["id"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_vector(case: dict[str, Any]):
    actual = RUNNERS[case["kind"]](case)
    wrong = {
        field: {"expected": expected, "actual": actual[field]}
        for field, expected in case["expect"].items()
        if not _same(field, actual[field], expected)
    }
    assert not wrong, f"{case['id']} {case['input']!r}: {wrong}"
