"""Meal plan entries and plan -> ingredients (architecture §4 Planning, §6.1; roadmap Phase 3).

All routes are household-scoped (`X-Thymble-Household`). Entries also sync through
PowerSync (`meal_plan_entry`); these routes are for REST clients and for the expansion,
which needs the server's scaling and merge rules.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from thymble import units
from thymble.api.deps import DbSession, Household
from thymble.models import MealPlanEntry, MealSlot, Recipe
from thymble.shopping import mealplan
from thymble.shopping.service import ShoppingService

router = APIRouter(prefix="/meal-plan", tags=["meal plan"])

MAX_RANGE_DAYS = 366


# ------------------------------------------------------------------------ schemas
class EntryIn(BaseModel):
    """A recipe or a free-text meal ("leftovers", "eat out") on a day and slot."""

    id: uuid.UUID | None = None  # client-generated, like offline creates; optional
    date: dt.date
    meal_slot: MealSlot
    recipe_id: uuid.UUID | None = None
    free_text: str | None = Field(default=None, max_length=500)
    servings: Decimal | None = Field(default=None, gt=0, max_digits=8, decimal_places=2)
    notes: str | None = None

    @model_validator(mode="after")
    def _recipe_or_text(self) -> EntryIn:
        if self.recipe_id is None and not (self.free_text and self.free_text.strip()):
            raise ValueError("give recipe_id or free_text")
        return self


class EntryPatch(BaseModel):
    """Only the fields sent change; send null to clear recipe_id, free_text, servings or
    notes (an entry must keep a recipe or free text)."""

    date: dt.date | None = None
    meal_slot: MealSlot | None = None
    recipe_id: uuid.UUID | None = None
    free_text: str | None = Field(default=None, max_length=500)
    servings: Decimal | None = Field(default=None, gt=0, max_digits=8, decimal_places=2)
    notes: str | None = None


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    date: dt.date
    meal_slot: MealSlot
    recipe_id: uuid.UUID | None
    recipe_title: str | None = None
    free_text: str | None
    servings: Decimal | None
    notes: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class SourceOut(BaseModel):
    meal_plan_entry_id: uuid.UUID
    date: dt.date
    meal_slot: MealSlot
    recipe_id: uuid.UUID
    recipe_title: str
    servings: Decimal | None
    recipe_ingredient_id: uuid.UUID
    raw_text: str
    quantity: Decimal | None
    quantity_max: Decimal | None
    unit: str | None


class LineOut(BaseModel):
    ingredient_id: uuid.UUID | None
    name: str
    quantity: Decimal | None
    unit: str | None
    # "2 cup milk", ready to show.
    text: str
    sources: list[SourceOut]


class ExpansionOut(BaseModel):
    start: dt.date
    end: dt.date
    lines: list[LineOut]
    # Entries that add no ingredients: free-text meals, or recipes since deleted.
    other_entries: list[EntryOut]


# ------------------------------------------------------------------------ helpers
def _range(start: dt.date, end: dt.date) -> None:
    if end < start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "end is before start")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"range is longer than {MAX_RANGE_DAYS} days"
        )


def _entry(db: Session, household_id: uuid.UUID, entry_id: uuid.UUID) -> MealPlanEntry:
    entry = db.get(MealPlanEntry, entry_id)
    if entry is None or entry.household_id != household_id or entry.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "meal plan entry not found")
    return entry


def _recipe(db: Session, household_id: uuid.UUID, recipe_id: uuid.UUID) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if recipe is None or recipe.household_id != household_id or recipe.deleted_at is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "recipe not found")
    return recipe


def _out(db: Session, entry: MealPlanEntry) -> EntryOut:
    out = EntryOut.model_validate(entry)
    if entry.recipe_id is not None:
        recipe = db.get(Recipe, entry.recipe_id)
        if recipe is not None and recipe.deleted_at is None:
            out.recipe_title = recipe.title
    return out


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


# ------------------------------------------------------------------------ routes
@router.get("")
def list_entries(
    db: DbSession,
    hh: Household,
    start: dt.date,
    end: dt.date,
) -> list[EntryOut]:
    """Entries from `start` to `end` (inclusive), by date and slot."""
    _range(start, end)
    return [_out(db, e) for e in mealplan.plan_entries(db, hh.household_id, start, end)]


@router.get("/ingredients")
def plan_ingredients(
    db: DbSession,
    hh: Household,
    start: dt.date,
    end: dt.date,
) -> ExpansionOut:
    """The plan's ingredients for a date range: scaled to each entry's servings, merged by
    ingredient and compatible unit (as on the shopping list), with every recipe's share.
    Incompatible units (cups vs grams) stay separate lines. Nothing is written."""
    _range(start, end)
    expansion = mealplan.expand_plan(db, hh.household_id, start, end)
    lines = []
    for pl in expansion.lines:
        amount = units.format_quantity(pl.quantity, pl.unit)
        sources = [
            SourceOut(
                meal_plan_entry_id=s.entry.id,
                date=s.entry.date,
                meal_slot=s.entry.meal_slot,
                recipe_id=s.recipe.id,
                recipe_title=s.recipe.title,
                servings=s.entry.servings,
                recipe_ingredient_id=s.scaled.line.id,
                raw_text=s.scaled.line.raw_text,
                quantity=s.scaled.quantity,
                quantity_max=s.scaled.quantity_max,
                unit=units.normalize(s.scaled.line.unit),
            )
            for s in pl.sources
        ]
        lines.append(
            LineOut(
                ingredient_id=pl.ingredient_id,
                name=pl.name,
                quantity=pl.quantity,
                unit=pl.unit,
                text=f"{amount} {pl.name}" if amount else pl.name,
                sources=sources,
            )
        )
    return ExpansionOut(
        start=start,
        end=end,
        lines=lines,
        other_entries=[_out(db, e) for e in expansion.other_entries],
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_entry(body: EntryIn, db: DbSession, hh: Household) -> EntryOut:
    if body.recipe_id is not None:
        _recipe(db, hh.household_id, body.recipe_id)
    if body.id is not None and db.get(MealPlanEntry, body.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "an entry with this id exists")
    entry = MealPlanEntry(
        household_id=hh.household_id,
        date=body.date,
        meal_slot=body.meal_slot,
        recipe_id=body.recipe_id,
        free_text=_clean_text(body.free_text),
        servings=body.servings,
        notes=body.notes,
    )
    if body.id is not None:
        entry.id = body.id
    db.add(entry)
    db.commit()
    db.refresh(entry)  # numeric values as stored ("8.00")
    return _out(db, entry)


@router.get("/{entry_id}")
def get_entry(entry_id: uuid.UUID, db: DbSession, hh: Household) -> EntryOut:
    return _out(db, _entry(db, hh.household_id, entry_id))


@router.patch("/{entry_id}")
def update_entry(entry_id: uuid.UUID, body: EntryPatch, db: DbSession, hh: Household) -> EntryOut:
    """Change an entry. A changed servings count or recipe does not change shopping lists
    the entry was already added to (re-add it there to update them)."""
    entry = _entry(db, hh.household_id, entry_id)
    sent = body.model_fields_set
    for required in ("date", "meal_slot"):
        if required in sent and getattr(body, required) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{required} can't be null")
    if "recipe_id" in sent and body.recipe_id is not None:
        _recipe(db, hh.household_id, body.recipe_id)
    values = body.model_dump(include=sent)
    if "free_text" in values:
        values["free_text"] = _clean_text(values["free_text"])
    for name, value in values.items():
        setattr(entry, name, value)
    if entry.recipe_id is None and entry.free_text is None:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "an entry needs a recipe or free text"
        )
    db.commit()
    db.refresh(entry)
    return _out(db, entry)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(entry_id: uuid.UUID, db: DbSession, hh: Household) -> None:
    """Soft delete. The entry's share comes off any shopping list it was added to (items
    already bought or on order are left alone)."""
    entry = _entry(db, hh.household_id, entry_id)
    mealplan.delete_entry(ShoppingService(db, hh.household_id, hh.user_id), entry)
    db.commit()
