"""Recipes, ingredients lines, steps and tags (architecture §4)."""

from __future__ import annotations

import enum
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, SyncedBase, str_enum


class RecipeSourceType(enum.StrEnum):
    URL = "url"
    PHOTO = "photo"
    TEXT = "text"
    MANUAL = "manual"


class Recipe(SyncedBase, Base):
    __tablename__ = "recipe"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),)

    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[RecipeSourceType] = mapped_column(
        str_enum(RecipeSourceType, "recipe_source_type"), default=RecipeSourceType.MANUAL
    )
    # Yield is kept raw ("Makes 24 cookies") plus a parsed amount for scaling.
    yield_text: Mapped[str | None] = mapped_column(Text)
    yield_quantity: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    yield_unit: Mapped[str | None] = mapped_column(String(64))
    prep_time_minutes: Mapped[int | None] = mapped_column(Integer)
    cook_time_minutes: Mapped[int | None] = mapped_column(Integer)
    total_time_minutes: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    # True while an imported recipe waits for the user's review (import pipeline §5.8).
    is_draft: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)


class RecipeIngredient(SyncedBase, Base):
    __tablename__ = "recipe_ingredient"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    # The original line, kept forever (import pipeline §5).
    raw_text: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    # Upper bound for ranges such as "2-3 cloves".
    quantity_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    # Canonical unit code ("g", "cup", "tbsp"); a `unit` table may replace this later.
    unit: Mapped[str | None] = mapped_column(String(32))
    ingredient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ingredient.id", ondelete="SET NULL"), index=True
    )
    preparation: Mapped[str | None] = mapped_column(Text)
    # Section heading, e.g. "For the sauce".
    group_name: Mapped[str | None] = mapped_column(Text)


class RecipeStep(SyncedBase, Base):
    __tablename__ = "recipe_step"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    group_name: Mapped[str | None] = mapped_column(Text)
    # Structured timers, e.g. [{"label": "bake", "seconds": 1500}].
    timers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)


class Tag(SyncedBase, Base):
    __tablename__ = "tag"

    name: Mapped[str] = mapped_column(Text)


class RecipeTag(SyncedBase, Base):
    __tablename__ = "recipe_tag"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), index=True)
