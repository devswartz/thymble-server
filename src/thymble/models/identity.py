"""Identity: users, households, membership (architecture §4)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, IdMixin, SoftDeleteMixin, SyncedBase, TimestampMixin, str_enum


class HouseholdRole(enum.StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class User(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """An account. `user` is reserved in Postgres, so the table is `app_user`.

    Users are not household-scoped (one user can belong to several households).
    """

    __tablename__ = "app_user"

    display_name: Mapped[str | None] = mapped_column(Text)
    # Stored lower-cased by the api; unique when present.
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sign in with Apple stable subject identifier.
    apple_sub: Mapped[str | None] = mapped_column(String(255), unique=True)


class Household(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Sharing boundary for recipes, plans and lists. Its own id is the sync scope."""

    __tablename__ = "household"

    name: Mapped[str] = mapped_column(Text)


class HouseholdMember(SyncedBase, Base):
    __tablename__ = "household_member"
    __table_args__ = (UniqueConstraint("household_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[HouseholdRole] = mapped_column(
        str_enum(HouseholdRole, "household_role"), default=HouseholdRole.MEMBER
    )
