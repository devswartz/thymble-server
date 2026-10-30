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


class ReviewState(enum.StrEnum):
    """Import review (D-034): an import with anything not "high" (and every photo import)
    starts in needs_review; POST /v1/imports/{id}/finalize moves it to reviewed."""

    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"


class ImportConfidence(enum.StrEnum):
    HIGH = "high"
    CHECK = "check"
    LOW = "low"


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
    # Imported recipes only (NULL for recipes typed in): needs_review until every "low"
    # item is confirmed or edited (D-034). Server-owned; read-only through sync.
    review_state: Mapped[ReviewState | None] = mapped_column(
        str_enum(ReviewState, "recipe_review_state")
    )
    # Section confidence (title, yield, times, groups, completeness) with reasons, the
    # import's counts, and once reviewed who reviewed it and when.
    review: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


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
    # Import confidence (D-034); NULL for lines typed in. Details in `review`: reasons,
    # reason codes, photo readings, and the review outcome. Server-owned.
    confidence: Mapped[ImportConfidence | None] = mapped_column(
        str_enum(ImportConfidence, "import_confidence")
    )
    review: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


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
    # Import confidence and its details, as on recipe_ingredient.
    confidence: Mapped[ImportConfidence | None] = mapped_column(
        str_enum(ImportConfidence, "import_confidence")
    )
    review: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Tag(SyncedBase, Base):
    __tablename__ = "tag"

    name: Mapped[str] = mapped_column(Text)


class RecipeTag(SyncedBase, Base):
    __tablename__ = "recipe_tag"

    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), index=True)
