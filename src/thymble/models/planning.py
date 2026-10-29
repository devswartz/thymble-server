"""Meal planning (architecture §4)."""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, SyncedBase, str_enum


class MealSlot(enum.StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class MealPlanEntry(SyncedBase, Base):
    __tablename__ = "meal_plan_entry"
    __table_args__ = (
        CheckConstraint(
            "recipe_id IS NOT NULL OR free_text IS NOT NULL", name="recipe_or_free_text"
        ),
    )

    date: Mapped[dt.date] = mapped_column(Date, index=True)
    meal_slot: Mapped[MealSlot] = mapped_column(str_enum(MealSlot, "meal_slot"))
    recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipe.id", ondelete="SET NULL"), index=True
    )
    free_text: Mapped[str | None] = mapped_column(Text)
    servings: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    notes: Mapped[str | None] = mapped_column(Text)
