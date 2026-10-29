"""Declarative base and shared column mixins."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Stable constraint names so Alembic migrations are deterministic.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def str_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    """Store an enum as VARCHAR + CHECK constraint rather than a native Postgres enum.

    Adding values later is a plain constraint change, and PowerSync / SQLite on the
    device see ordinary text.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )


class IdMixin:
    """UUID primary key. Clients may generate it (offline creates); else the server does."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, sort_order=-100)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), sort_order=100
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), sort_order=101
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), sort_order=102)


class HouseholdScopedMixin:
    """Rows that belong to one household; PowerSync sync streams filter on this column."""

    @declared_attr
    def household_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            ForeignKey("household.id", ondelete="CASCADE"), index=True, sort_order=-90
        )


class SyncedBase(IdMixin, HouseholdScopedMixin, TimestampMixin, SoftDeleteMixin):
    """Columns every synced row carries (architecture §4)."""
