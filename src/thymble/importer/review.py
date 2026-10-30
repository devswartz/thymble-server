"""Finalizing an import review (D-034; architecture §5.8 "Review").

`POST /v1/imports/{id}/finalize` lists what the user decided, item by item (ingredients
and steps by index in the import result, sections by field):

  confirmed  "this is right as shown"            settles any item
  accepted   the "accept all" button             settles "check" items only
  edited     with the corrected values           settles any item

The server refuses to finalize while any "low" item is not confirmed or edited, or any
"check" item is not decided at all; nothing is written then. Every decision becomes a
`learning_event`, which is what the confidence thresholds are re-tuned from:

  import_corrected  one per changed field: original vs edited value, the item's
                    confidence and reason codes
  import_confirmed  one per confirmed item
  import_reviewed   one per recipe: every item's confidence, reason codes and outcome
                    (edited / confirmed / accepted / unchanged), for calibration

The completeness section (missing steps, lines only the phone read) is reported but is not
something the user can confirm field by field, so it never blocks finalizing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from thymble.importer import rules

SECTION_FIELDS = ("title", "yield", "times", "groups")
INGREDIENT_FIELDS = (
    "quantity",
    "quantity_max",
    "unit",
    "name",
    "additional",
    "alternatives",
    "preparation",
    "note",
    "optional",
)


class ReviewError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


@dataclass
class Decision:
    target: str  # ingredient | step | section
    key: int | str  # index for ingredients and steps, field name for sections
    action: str  # confirmed | accepted | edited
    confidence: str | None = None  # what the client showed (recorded, not trusted)
    ingredient: dict[str, Any] = field(default_factory=dict)
    text: str | None = None
    value: Any = None


@dataclass
class _Item:
    target: str
    key: int | str
    confidence: str
    reasons: list[str]
    reason_codes: list[str]
    row: Any = None  # RecipeIngredient | RecipeStep; None for sections
    original: dict[str, Any] = field(default_factory=dict)  # as imported

    def ref(self) -> dict[str, Any]:
        return {"target": self.target, ("field" if self.target == "section" else "index"): self.key}


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _same(a: Any, b: Any) -> bool:
    a, b = _plain(a), _plain(b)
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _items(recipe: Any, lines: list[Any], steps: list[Any], result: dict) -> list[_Item]:
    sections = (recipe.review or {}).get("sections") or {}
    parsed = result.get("ingredients") or []
    items = [
        _Item(
            "section",
            name,
            sections[name].get("confidence", "high"),
            sections[name].get("reasons", []),
            sections[name].get("reason_codes", []),
        )
        for name in SECTION_FIELDS
        if name in sections
    ]
    for target, rows in (("ingredient", lines), ("step", steps)):
        for row in rows:
            extra = row.review or {}
            original: dict[str, Any] = {}
            if target == "ingredient" and row.position < len(parsed):
                original = {k: parsed[row.position].get(k) for k in INGREDIENT_FIELDS}
            items.append(
                _Item(
                    target,
                    row.position,
                    str(row.confidence or "high"),
                    extra.get("reasons", []),
                    extra.get("reason_codes", []),
                    row,
                    original,
                )
            )
    return items


def _load(db: Session, household_id: uuid.UUID, recipe_id: uuid.UUID) -> tuple[Any, list, list]:
    from thymble.models import Recipe, RecipeIngredient, RecipeStep

    recipe = db.get(Recipe, recipe_id, with_for_update=True)
    if recipe is None or recipe.household_id != household_id or recipe.deleted_at is not None:
        raise ReviewError(409, "the imported recipe has been deleted")
    lines = list(
        db.scalars(
            select(RecipeIngredient)
            .where(RecipeIngredient.recipe_id == recipe.id, RecipeIngredient.deleted_at.is_(None))
            .order_by(RecipeIngredient.position)
        )
    )
    steps = list(
        db.scalars(
            select(RecipeStep)
            .where(RecipeStep.recipe_id == recipe.id, RecipeStep.deleted_at.is_(None))
            .order_by(RecipeStep.position)
        )
    )
    return recipe, lines, steps


def _ingredient_for(db: Session, household_id: uuid.UUID, name: str) -> uuid.UUID:
    """The household's or the global catalog's ingredient by name or alias, else a new
    household ingredient under that name."""
    from thymble.models import Ingredient, IngredientAlias

    key = " ".join(name.lower().split())
    found = db.scalar(
        select(Ingredient.id)
        .where(
            func.lower(Ingredient.name) == key,
            Ingredient.deleted_at.is_(None),
            (Ingredient.household_id == household_id) | Ingredient.household_id.is_(None),
        )
        .order_by(Ingredient.household_id.is_(None))
        .limit(1)
    )
    if found is None:
        found = db.scalar(
            select(IngredientAlias.ingredient_id)
            .where(
                IngredientAlias.alias == key,
                IngredientAlias.deleted_at.is_(None),
                (IngredientAlias.household_id == household_id)
                | IngredientAlias.household_id.is_(None),
            )
            .order_by(IngredientAlias.household_id.is_(None))
            .limit(1)
        )
    if found is None:
        ingredient = Ingredient(household_id=household_id, name=key)
        db.add(ingredient)
        db.flush()
        found = ingredient.id
    return found


def _section_changes(recipe: Any, name: str, value: Any) -> dict[str, tuple[str, Any, Any]]:
    """field -> (recipe column, original, edited) for a section edit."""
    if name == "title":
        return {"title": ("title", recipe.title, value)}
    if name == "yield":
        if isinstance(value, dict):
            parsed = {k: value.get(k) for k in ("text", "quantity", "unit")}
        else:
            parsed = rules.parse_yield(value)
        quantity = parsed.get("quantity")
        return {
            "yield_text": ("yield_text", recipe.yield_text, parsed.get("text")),
            "yield_quantity": (
                "yield_quantity",
                recipe.yield_quantity,
                None if quantity is None else Decimal(str(quantity)),
            ),
            "yield_unit": ("yield_unit", recipe.yield_unit, parsed.get("unit")),
        }
    if name == "times":
        out = {}
        for key in ("prep", "cook", "total"):
            if f"{key}_minutes" in value:
                column = f"{key}_time_minutes"
                out[f"{key}_minutes"] = (column, getattr(recipe, column), value[f"{key}_minutes"])
        return out
    return {}  # groups: recorded as a correction; the groups themselves live on lines/steps


def finalize(
    db: Session,
    *,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    recipe_id: uuid.UUID,
    result: dict[str, Any],
    import_id: uuid.UUID | None,
    decisions: list[Decision],
) -> dict[str, Any]:
    """Apply the decisions and mark the recipe reviewed, or raise ReviewError (the caller
    rolls back). Returns the outcome counts."""
    from thymble.models import LearningEvent, ReviewState

    recipe, lines, steps = _load(db, household_id, recipe_id)
    if recipe.review_state is None:
        raise ReviewError(409, "this recipe was not imported, so there is nothing to review")
    if recipe.review_state == ReviewState.REVIEWED:
        raise ReviewError(409, "this import has already been finalized")

    items = {(i.target, i.key): i for i in _items(recipe, lines, steps, result)}
    decided: dict[tuple[str, int | str], Decision] = {}
    for d in decisions:
        key = (d.target, d.key)
        if key not in items:
            raise ReviewError(422, f"{d.target} {d.key} is not part of this import")
        if key in decided:
            raise ReviewError(422, f"{d.target} {d.key} is listed twice")
        decided[key] = d

    unresolved = []
    for key, item in items.items():
        d = decided.get(key)
        if (item.confidence == "low" and (d is None or d.action == "accepted")) or (
            item.confidence == "check" and d is None
        ):
            unresolved.append(item)
    if unresolved:
        raise ReviewError(
            409,
            {
                "error": "confirm or edit every low item, and decide every check item",
                "unresolved": [
                    {**i.ref(), "confidence": i.confidence, "reasons": i.reasons}
                    for i in unresolved
                ],
            },
        )

    source_kind = (recipe.review or {}).get("source_kind")
    events: list[LearningEvent] = []
    outcomes: dict[tuple[str, int | str], str] = {}

    def event(event_type: str, item: _Item, d: Decision, payload: dict[str, Any]) -> None:
        subject = {"ingredient": "recipe_ingredient", "step": "recipe_step"}.get(
            item.target, "recipe"
        )
        events.append(
            LearningEvent(
                household_id=household_id,
                user_id=user_id,
                event_type=event_type,
                subject_type=subject,
                subject_id=item.row.id if item.row is not None else recipe.id,
                payload={
                    "recipe_id": str(recipe.id),
                    "import_id": str(import_id) if import_id else None,
                    "source_kind": source_kind,
                    **item.ref(),
                    **({"raw_text": item.row.raw_text} if item.target == "ingredient" else {}),
                    "confidence": item.confidence,
                    "client_confidence": d.confidence,
                    "reason_codes": item.reason_codes,
                    **payload,
                },
            )
        )

    def change(item: _Item, d: Decision, name: str, original: Any, edited: Any) -> bool:
        if _same(original, edited):
            return False
        event(
            "import_corrected",
            item,
            d,
            {"field": name, "original": _plain(original), "edited": _plain(edited)},
        )
        return True

    for key, d in decided.items():
        item = items[key]
        changed = False
        if d.action == "edited" and item.target == "ingredient":
            changed = _edit_ingredient(db, household_id, item, d, change)
        elif d.action == "edited" and item.target == "step":
            changed = change(item, d, "text", item.row.text, d.text)
            item.row.text = d.text
        elif d.action == "edited":
            if item.key == "groups":
                sections = (recipe.review or {}).get("sections") or {}
                original = sorted((sections.get("groups") or {}).get("names") or [])
                changed = change(item, d, "groups", original, d.value)
            for name, (column, original, edited) in _section_changes(
                recipe, str(item.key), d.value
            ).items():
                changed |= change(item, d, name, original, edited)
                setattr(recipe, column, edited)
        if changed:
            outcomes[key] = "edited"
        elif d.action == "accepted":
            outcomes[key] = "accepted"
        else:
            outcomes[key] = "confirmed"
            event("import_confirmed", item, d, {"action": d.action})

    for key, item in items.items():
        outcomes.setdefault(key, "unchanged")
        if item.row is not None:
            item.row.review = {**(item.row.review or {}), "outcome": outcomes[key]}

    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    review = dict(recipe.review or {})
    review["sections"] = {
        name: {**data, "outcome": outcomes.get(("section", name), "unchanged")}
        for name, data in (review.get("sections") or {}).items()
    }
    review.update(
        reviewed_at=datetime.now(UTC).isoformat(), reviewed_by=str(user_id), outcomes=counts
    )
    recipe.review = review
    recipe.review_state = ReviewState.REVIEWED
    recipe.is_draft = False

    events.append(
        LearningEvent(
            household_id=household_id,
            user_id=user_id,
            event_type="import_reviewed",
            subject_type="recipe",
            subject_id=recipe.id,
            payload={
                "recipe_id": str(recipe.id),
                "import_id": str(import_id) if import_id else None,
                "source_kind": source_kind,
                "outcomes": counts,
                "items": [
                    {
                        **item.ref(),
                        "confidence": item.confidence,
                        "reason_codes": item.reason_codes,
                        "outcome": outcomes[key],
                    }
                    for key, item in items.items()
                ],
            },
        )
    )
    db.add_all(events)
    return {"outcomes": counts, "learning_events": len(events)}


def _edit_ingredient(
    db: Session, household_id: uuid.UUID, item: _Item, d: Decision, change
) -> bool:
    """Apply an ingredient edit. Amount, unit and preparation go to the row; a new name is
    linked to the catalog; the rest (additional, alternatives, note, optional) has no column
    yet and is kept in the row's review details and the learning events."""
    row, new = item.row, d.ingredient
    changed = False
    for name in INGREDIENT_FIELDS:
        if name not in new:
            continue
        value = new[name]
        if name in ("quantity", "quantity_max"):
            value = None if value is None else Decimal(str(value))
            changed |= change(item, d, name, getattr(row, name), value)
            setattr(row, name, value)
        elif name == "unit":
            value = rules.norm_unit(value)
            changed |= change(item, d, name, row.unit, value)
            row.unit = value
        elif name == "preparation":
            changed |= change(item, d, name, row.preparation, value)
            row.preparation = value
        elif change(item, d, name, item.original.get(name), value):
            changed = True
            if name == "name" and value:
                row.ingredient_id = _ingredient_for(db, household_id, value)
    if changed:
        row.review = {**(row.review or {}), "edited": new}
    return changed
