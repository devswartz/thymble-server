"""Which tables devices may write through the PowerSync upload endpoint, and how.

Register a table once (at import time) and the upload endpoint will accept PUT / PATCH /
DELETE for it:

    from thymble.sync import SyncTable, register
    register(SyncTable(MealPlanEntry))

By default every column except the system columns (`id`, `household_id`, `created_at`,
`updated_at`, `deleted_at`) is writable. Narrow it with `columns=` (allow-list) or
`readonly=` (deny-list), and set server-owned values in `before_write`.

Every write is checked generically, whatever the table:
- the row's household must be one the user belongs to; rows never move households;
- every foreign key that points at a household-scoped row must point into the same
  household (global catalog rows with a NULL household are allowed);
- a foreign key to `app_user` must name a member of the same household.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.orm import Session

from thymble.db.base import Base
from thymble.models import User

Op = Literal["PUT", "PATCH", "DELETE"]

SYSTEM_COLUMNS = frozenset({"id", "household_id", "created_at", "updated_at", "deleted_at"})


@dataclass
class WriteContext:
    db: Session
    user: User
    household_ids: set[uuid.UUID]


# (context, row, op, inserting, changes) -> None. Runs after client values are applied.
BeforeWrite = Callable[[WriteContext, Any, Op, bool, dict[str, Any]], None]


@dataclass(frozen=True)
class SyncTable:
    model: type[Base]
    # Allow-list of client-writable columns; None means "all but the system columns".
    columns: frozenset[str] | None = None
    # Columns clients may not write even though they'd otherwise be allowed.
    readonly: frozenset[str] = field(default_factory=frozenset)
    before_write: BeforeWrite | None = None

    @property
    def name(self) -> str:
        return self.model.__tablename__

    @property
    def writable(self) -> frozenset[str]:
        table_columns = {c.name for c in self.model.__table__.columns}
        allowed = self.columns if self.columns is not None else frozenset(table_columns)
        unknown = allowed - table_columns
        if unknown:
            raise ValueError(f"{self.name}: unknown columns {sorted(unknown)}")
        return frozenset(allowed - SYSTEM_COLUMNS - self.readonly)


_REGISTRY: dict[str, SyncTable] = {}


def register(table: SyncTable) -> SyncTable:
    if "household_id" not in table.model.__table__.columns:
        raise ValueError(f"{table.name} has no household_id; only household rows can sync up")
    table.writable  # noqa: B018 - validate column names at registration time
    _REGISTRY[table.name] = table
    return table


def get_table(name: str) -> SyncTable | None:
    return _REGISTRY.get(name)


def registered_tables() -> dict[str, SyncTable]:
    return dict(_REGISTRY)
