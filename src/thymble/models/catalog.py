"""Ingredient catalog (architecture §4, §6.4).

Catalog rows are either global (household_id NULL, seeded and shared by everyone) or
household-specific (a household's own items and aliases learned from corrections).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin


class _OptionalHousehold:
    household_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), index=True, sort_order=-90
    )


class Ingredient(IdMixin, _OptionalHousehold, TimestampMixin, SoftDeleteMixin, Base):
    """A canonical item ("green onion"); also covers non-grocery items ("paper towels")."""

    __tablename__ = "ingredient"

    name: Mapped[str] = mapped_column(Text, index=True)
    # produce, dairy, household, ...
    category: Mapped[str | None] = mapped_column(String(64))
    default_aisle_category: Mapped[str | None] = mapped_column(String(64))


class IngredientAlias(IdMixin, _OptionalHousehold, TimestampMixin, SoftDeleteMixin, Base):
    """Alternate name for an ingredient ("scallion" -> green onion)."""

    __tablename__ = "ingredient_alias"

    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredient.id", ondelete="CASCADE"), index=True
    )
    # Normalized (lower-cased, trimmed) alias text.
    alias: Mapped[str] = mapped_column(Text, index=True)
