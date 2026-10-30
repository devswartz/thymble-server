"""Recipes over REST: list and search, read with lines/steps/tags, create, edit, soft
delete, and a scaled preview (architecture §4 Recipes).

The app reads and edits recipes through PowerSync; these routes are for REST clients
(the Phase 1 exit test) and for anything that wants the server's scaling. All routes are
household-scoped (`X-Thymble-Household`). Edits keep line and step ids stable when the
client sends them back, so synced devices see updates rather than delete + insert.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from thymble import units
from thymble.api.deps import DbSession, Household
from thymble.models import (
    Ingredient,
    Recipe,
    RecipeIngredient,
    RecipeSourceType,
    RecipeStep,
    RecipeTag,
    Tag,
)
from thymble.shopping import mealplan
from thymble.shopping.service import utcnow

router = APIRouter(prefix="/recipes", tags=["recipes"])

MAX_PAGE = 200


# ------------------------------------------------------------------------ schemas
class IngredientLineIn(BaseModel):
    """One ingredient line. Send `id` to keep an existing line; lines left out are removed."""

    id: uuid.UUID | None = None
    raw_text: str = Field(min_length=1)
    quantity: Decimal | None = Field(default=None, ge=0)
    quantity_max: Decimal | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=32)
    ingredient_id: uuid.UUID | None = None
    preparation: str | None = None
    group_name: str | None = None


class StepIn(BaseModel):
    id: uuid.UUID | None = None
    text: str = Field(min_length=1)
    group_name: str | None = None
    timers: list[dict[str, Any]] | None = None


class RecipeFields(BaseModel):
    description: str | None = None
    source_url: str | None = None
    yield_text: str | None = None
    yield_quantity: Decimal | None = Field(default=None, gt=0)
    yield_unit: str | None = Field(default=None, max_length=64)
    prep_time_minutes: int | None = Field(default=None, ge=0)
    cook_time_minutes: int | None = Field(default=None, ge=0)
    total_time_minutes: int | None = Field(default=None, ge=0)
    image_url: str | None = None
    notes: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    is_draft: bool | None = None
    tags: list[str] | None = None
    ingredients: list[IngredientLineIn] | None = None
    steps: list[StepIn] | None = None


class RecipeIn(RecipeFields):
    title: str = Field(min_length=1)


class RecipePatch(RecipeFields):
    """Only the fields sent change. `tags`, `ingredients` and `steps` replace the whole
    list when sent (send ids back to keep existing lines and steps)."""

    title: str | None = Field(default=None, min_length=1)


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RecipeSummary(_Out):
    id: uuid.UUID
    title: str
    description: str | None
    source_type: RecipeSourceType
    source_url: str | None
    image_url: str | None
    yield_text: str | None
    yield_quantity: Decimal | None
    yield_unit: str | None
    total_time_minutes: int | None
    rating: int | None
    is_draft: bool
    tags: list[str] = []
    created_at: dt.datetime
    updated_at: dt.datetime


class IngredientLineOut(_Out):
    id: uuid.UUID
    position: int
    raw_text: str
    quantity: Decimal | None
    quantity_max: Decimal | None
    unit: str | None
    ingredient_id: uuid.UUID | None
    ingredient_name: str | None = None
    preparation: str | None
    group_name: str | None
    # At the requested servings (same as quantity when none were asked for).
    scaled_quantity: Decimal | None = None
    scaled_quantity_max: Decimal | None = None


class StepOut(_Out):
    id: uuid.UUID
    position: int
    text: str
    group_name: str | None
    timers: list[dict[str, Any]] | None


class RecipeOut(RecipeSummary):
    prep_time_minutes: int | None
    cook_time_minutes: int | None
    notes: str | None
    created_by: uuid.UUID | None
    ingredients: list[IngredientLineOut] = []
    steps: list[StepOut] = []
    # Scaled preview: servings asked for and the factor applied (1 without a yield).
    servings: Decimal | None = None
    scale: Decimal = Decimal(1)


# ------------------------------------------------------------------------ helpers
def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


def _get(db: Session, household_id: uuid.UUID, recipe_id: uuid.UUID) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if recipe is None or recipe.household_id != household_id or recipe.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recipe not found")
    return recipe


def _live(db: Session, model, recipe_id: uuid.UUID) -> list:
    q = select(model).where(model.recipe_id == recipe_id, model.deleted_at.is_(None))
    if hasattr(model, "position"):
        q = q.order_by(model.position, model.id)
    return list(db.scalars(q))


def _tag_names(db: Session, recipe_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not recipe_ids:
        return {}
    rows = db.execute(
        select(RecipeTag.recipe_id, Tag.name)
        .join(Tag, Tag.id == RecipeTag.tag_id)
        .where(
            RecipeTag.recipe_id.in_(recipe_ids),
            RecipeTag.deleted_at.is_(None),
            Tag.deleted_at.is_(None),
        )
        .order_by(func.lower(Tag.name))
    )
    names: dict[uuid.UUID, list[str]] = {}
    for recipe_id, name in rows:
        names.setdefault(recipe_id, []).append(name)
    return names


def _detail(
    db: Session, recipe: Recipe, servings: Decimal | None = None, scale: Decimal | None = None
) -> RecipeOut:
    scale = scale if scale is not None else mealplan.scale_factor(recipe, servings)
    out = RecipeOut.model_validate(recipe)
    out.tags = _tag_names(db, [recipe.id]).get(recipe.id, [])
    out.servings, out.scale = servings, scale
    for scaled in mealplan.scaled_lines(db, recipe, scale):
        line = IngredientLineOut.model_validate(scaled.line)
        line.ingredient_name = scaled.ingredient.name if scaled.ingredient else None
        line.scaled_quantity, line.scaled_quantity_max = scaled.quantity, scaled.quantity_max
        out.ingredients.append(line)
    out.steps = [StepOut.model_validate(s) for s in _live(db, RecipeStep, recipe.id)]
    return out


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _set_tags(db: Session, recipe: Recipe, names: list[str]) -> None:
    wanted: dict[str, str] = {}
    for name in names:
        clean = " ".join(name.split())
        if clean:
            wanted.setdefault(clean.lower(), clean)
    tags = (
        {
            t.name.lower(): t
            for t in db.scalars(
                select(Tag).where(
                    Tag.household_id == recipe.household_id,
                    Tag.deleted_at.is_(None),
                    func.lower(Tag.name).in_(list(wanted)),
                )
            )
        }
        if wanted
        else {}
    )
    for key, name in wanted.items():
        if key not in tags:
            tags[key] = Tag(household_id=recipe.household_id, name=name)
            db.add(tags[key])
    db.flush()
    wanted_ids = {tags[key].id for key in wanted}
    linked = {link.tag_id: link for link in _live(db, RecipeTag, recipe.id)}
    for tag_id, link in linked.items():
        if tag_id not in wanted_ids:
            link.deleted_at = utcnow()
    for tag_id in wanted_ids - linked.keys():
        db.add(RecipeTag(household_id=recipe.household_id, recipe_id=recipe.id, tag_id=tag_id))


def _check_ingredient(db: Session, recipe: Recipe, ingredient_id: uuid.UUID | None) -> None:
    if ingredient_id is None:
        return
    ing = db.get(Ingredient, ingredient_id)
    if (
        ing is None
        or ing.deleted_at is not None
        or ing.household_id not in (None, recipe.household_id)
    ):
        raise _unprocessable(f"ingredient {ingredient_id} not found")


def _replace(db: Session, recipe: Recipe, model, items: list[BaseModel], fields) -> None:
    """Make the recipe's live rows of `model` match `items`, in order, keeping sent ids."""
    existing = {row.id: row for row in _live(db, model, recipe.id)}
    seen: set[uuid.UUID] = set()
    for position, item in enumerate(items):
        values = fields(item)  # validates (may query) before anything is added
        if item.id is not None:
            if item.id not in existing or item.id in seen:
                raise _unprocessable(f"{model.__tablename__} {item.id} is not on this recipe")
            row = existing[item.id]
            seen.add(item.id)
        else:
            row = model(household_id=recipe.household_id, recipe_id=recipe.id)
        row.position = position
        for name, value in values.items():
            setattr(row, name, value)
        db.add(row)
    now = utcnow()
    for row_id, row in existing.items():
        if row_id not in seen:
            row.deleted_at = now


def _line_fields(db: Session, recipe: Recipe):
    def fields(line: IngredientLineIn) -> dict[str, Any]:
        _check_ingredient(db, recipe, line.ingredient_id)
        if line.quantity_max is not None and (
            line.quantity is None or line.quantity_max < line.quantity
        ):
            raise _unprocessable("quantity_max needs a quantity no larger than it")
        return {
            "raw_text": line.raw_text.strip(),
            "quantity": line.quantity,
            "quantity_max": line.quantity_max,
            "unit": units.normalize(line.unit),
            "ingredient_id": line.ingredient_id,
            "preparation": _clean(line.preparation),
            "group_name": _clean(line.group_name),
        }

    return fields


def _step_fields(step: StepIn) -> dict[str, Any]:
    return {"text": step.text.strip(), "group_name": _clean(step.group_name), "timers": step.timers}


_SCALAR_FIELDS = (
    "title",
    "description",
    "source_url",
    "yield_text",
    "yield_quantity",
    "yield_unit",
    "prep_time_minutes",
    "cook_time_minutes",
    "total_time_minutes",
    "image_url",
    "notes",
    "rating",
    "is_draft",
)


def _apply(db: Session, recipe: Recipe, body: RecipeFields, sent: set[str]) -> None:
    for name in _SCALAR_FIELDS:
        if name not in sent:
            continue
        value = getattr(body, name)
        if name in ("title", "is_draft") and value is None:
            raise _unprocessable(f"{name} can't be null")
        if name == "title":
            value = value.strip()
        setattr(recipe, name, value)
    if "tags" in sent and body.tags is not None:
        _set_tags(db, recipe, body.tags)
    if "ingredients" in sent and body.ingredients is not None:
        _replace(db, recipe, RecipeIngredient, body.ingredients, _line_fields(db, recipe))
    if "steps" in sent and body.steps is not None:
        _replace(db, recipe, RecipeStep, body.steps, _step_fields)


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ------------------------------------------------------------------------ routes
@router.get("")
def list_recipes(
    db: DbSession,
    hh: Household,
    q: Annotated[str | None, Query(description="Words in the title")] = None,
    tag: Annotated[str | None, Query(description="Tag name (any case)")] = None,
    draft: Annotated[bool | None, Query(description="Only drafts, or only reviewed")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RecipeSummary]:
    """The household's recipes by title. `q` matches every word in the title; `tag` keeps
    recipes with that tag."""
    query = select(Recipe).where(
        Recipe.household_id == hh.household_id, Recipe.deleted_at.is_(None)
    )
    for word in (q or "").split():
        query = query.where(Recipe.title.ilike(_like(word), escape="\\"))
    if tag and tag.strip():
        tagged = (
            select(RecipeTag.recipe_id)
            .join(Tag, Tag.id == RecipeTag.tag_id)
            .where(
                RecipeTag.household_id == hh.household_id,
                RecipeTag.deleted_at.is_(None),
                Tag.deleted_at.is_(None),
                func.lower(Tag.name) == " ".join(tag.split()).lower(),
            )
        )
        query = query.where(Recipe.id.in_(tagged))
    if draft is not None:
        query = query.where(Recipe.is_draft.is_(draft))
    query = query.order_by(func.lower(Recipe.title), Recipe.id).limit(limit).offset(offset)
    recipes = list(db.scalars(query))
    tags = _tag_names(db, [r.id for r in recipes])
    out = []
    for recipe in recipes:
        summary = RecipeSummary.model_validate(recipe)
        summary.tags = tags.get(recipe.id, [])
        out.append(summary)
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
def create_recipe(body: RecipeIn, db: DbSession, hh: Household) -> RecipeOut:
    """A recipe typed in by hand (imports go through /v1/imports)."""
    recipe = Recipe(
        household_id=hh.household_id,
        title=body.title.strip(),
        source_type=RecipeSourceType.MANUAL,
        created_by=hh.user_id,
        is_draft=False,
    )
    db.add(recipe)
    db.flush()
    _apply(db, recipe, body, body.model_fields_set - {"title"})
    db.commit()
    return _detail(db, recipe)


@router.get("/{recipe_id}")
def get_recipe(
    recipe_id: uuid.UUID,
    db: DbSession,
    hh: Household,
    servings: Annotated[
        Decimal | None,
        Query(gt=0, le=10000, description="Scale the lines to this many servings"),
    ] = None,
) -> RecipeOut:
    """The recipe with its ingredient lines, steps and tags. With `servings`, each line also
    carries `scaled_quantity` for that many servings (scaled from the recipe's yield; a
    recipe without a numeric yield isn't scaled and reports `scale` 1)."""
    return _detail(db, _get(db, hh.household_id, recipe_id), servings)


@router.patch("/{recipe_id}")
def update_recipe(
    recipe_id: uuid.UUID, body: RecipePatch, db: DbSession, hh: Household
) -> RecipeOut:
    recipe = _get(db, hh.household_id, recipe_id)
    try:
        _apply(db, recipe, body, body.model_fields_set)
    except HTTPException:
        db.rollback()
        raise
    recipe.updated_at = func.now()  # lines may change without the recipe row itself
    db.commit()
    db.refresh(recipe)
    return _detail(db, recipe)


@router.delete("/{recipe_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recipe(recipe_id: uuid.UUID, db: DbSession, hh: Household) -> None:
    """Soft delete, with its lines, steps and tag links (so devices drop them too). Plan
    entries that use it stay; they list it under `other_entries` in the plan expansion."""
    recipe = _get(db, hh.household_id, recipe_id)
    now = utcnow()
    for model in (RecipeIngredient, RecipeStep, RecipeTag):
        for row in _live(db, model, recipe.id):
            row.deleted_at = now
    recipe.deleted_at = now
    db.commit()
