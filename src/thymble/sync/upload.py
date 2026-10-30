"""Apply a batch of client changes from PowerSync's upload queue.

PowerSync clients queue local writes as CRUD entries (`op` PUT / PATCH / DELETE, `table`,
`id`, `opData`) and hand them to our backend in order. Semantics here:
- PUT    insert the row, or update it if the id exists (only the columns sent change);
         a soft-deleted row comes back to life.
- PATCH  update the columns sent; the row must exist.
- DELETE soft delete (sets `deleted_at`, or runs the table's `on_delete`); deleting a
         missing or deleted row is a no-op, so retries are safe.

The whole batch is one transaction: any invalid entry rejects the batch with a 4xx whose
detail names the entry, and nothing is written. 4xx means "this batch will never apply"
(the client should report and discard it); 5xx means "try again".
"""

from __future__ import annotations

import datetime as dt
import json
import math
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Column, Enum, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from thymble.auth.tokens import now
from thymble.models import HouseholdMember, User
from thymble.sync.registry import Op, SyncTable, WriteContext, WriteRejected, get_table


class UploadError(Exception):
    def __init__(self, status_code: int, index: int, entry: Any, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = {
            "index": index,
            "op": getattr(entry, "op", None),
            "table": getattr(entry, "table", None),
            "id": getattr(entry, "id", None),
            "error": message,
        }


class _Invalid(Exception):
    """Entry is malformed (422)."""


class _Forbidden(Exception):
    """Entry touches a household the user isn't in (403)."""


# --- value coercion --------------------------------------------------------------------
# Values arrive as JSON from the device's SQLite: UUIDs and timestamps are text, booleans
# are 0/1, JSON columns may be text. Coerce to what the column expects, or reject.


def _coerce(column: Column, value: Any) -> Any:
    if value is None:
        if not column.nullable:
            raise _Invalid(f"{column.name} can't be null")
        return None
    col_type = column.type
    if isinstance(col_type, Enum) and col_type.enum_class is not None:
        try:
            return col_type.enum_class(value)
        except ValueError as exc:
            raise _Invalid(f"{column.name}: {value!r} is not one of {col_type.enums}") from exc
    if isinstance(col_type, JSONB):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise _Invalid(f"{column.name}: not valid JSON") from exc
        if isinstance(value, dict | list):
            return value
        raise _Invalid(f"{column.name}: expected JSON")
    try:
        python_type = col_type.python_type
    except NotImplementedError as exc:
        raise _Invalid(f"{column.name}: unsupported column type") from exc
    try:
        return _coerce_python(python_type, value, getattr(col_type, "length", None))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise _Invalid(f"{column.name}: {exc}") from exc


def _coerce_python(python_type: type, value: Any, length: int | None) -> Any:
    if python_type is bool:
        if isinstance(value, bool) or value in (0, 1):
            return bool(value)
        raise ValueError(f"expected a boolean, got {value!r}")
    if isinstance(value, bool):
        raise ValueError(f"unexpected boolean {value!r}")
    if python_type is uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    if python_type is int:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, int) or (isinstance(value, str) and value.lstrip("-").isdigit()):
            return int(value)
        raise ValueError(f"expected an integer, got {value!r}")
    if python_type is Decimal:
        if not isinstance(value, int | float | str):
            raise ValueError(f"expected a number, got {value!r}")
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("expected a finite number")
        return number
    if python_type is float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("expected a finite number")
        return number
    if python_type is dt.datetime:
        if not isinstance(value, str):
            raise ValueError("expected an ISO 8601 timestamp")
        parsed = dt.datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    if python_type is dt.date:
        if not isinstance(value, str):
            raise ValueError("expected an ISO 8601 date")
        return dt.date.fromisoformat(value)
    if python_type is str:
        if not isinstance(value, str):
            raise ValueError(f"expected text, got {value!r}")
        if length is not None and len(value) > length:
            raise ValueError(f"longer than {length} characters")
        return value
    if python_type is bytes:
        raise ValueError("binary columns can't be written by clients")
    raise ValueError(f"unsupported type {python_type.__name__}")


# --- reference checks ------------------------------------------------------------------


def _check_references(ctx: WriteContext, table: SyncTable, row: Any, changed: set[str]) -> None:
    """Foreign keys may only point into the row's own household."""
    for column in table.model.__table__.columns:
        if column.name not in changed or column.name == "household_id":
            continue
        value = getattr(row, column.name)
        if value is None:
            continue
        for fk in column.foreign_keys:
            target = fk.column.table
            if target.name == User.__tablename__:
                is_member = ctx.db.scalar(
                    select(HouseholdMember.id).where(
                        HouseholdMember.household_id == row.household_id,
                        HouseholdMember.user_id == value,
                        HouseholdMember.deleted_at.is_(None),
                    )
                )
                if is_member is None:
                    raise _Invalid(f"{column.name}: not a member of this household")
                continue
            if "household_id" not in target.columns:
                continue
            found = ctx.db.execute(
                select(target.c.household_id).where(fk.column == value)
            ).one_or_none()
            if found is None:
                raise _Invalid(f"{column.name}: referenced {target.name} {value} doesn't exist")
            owner = found[0]
            if owner != row.household_id and not (owner is None and target.c.household_id.nullable):
                raise _Invalid(f"{column.name}: referenced {target.name} is in another household")


# --- applying entries ------------------------------------------------------------------


def _changes(table: SyncTable, data: dict[str, Any]) -> dict[str, Any]:
    writable = table.writable
    columns = table.model.__table__.columns
    changes: dict[str, Any] = {}
    for name, value in data.items():
        if name in ("id", "household_id"):
            continue
        if name not in writable:
            raise _Invalid(f"column {name!r} is not writable")
        changes[name] = _coerce(columns[name], value)
    return changes


def _household_from(data: dict[str, Any]) -> uuid.UUID | None:
    raw = data.get("household_id")
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise _Invalid("household_id is not a UUID") from exc


def _apply(ctx: WriteContext, op: Op, table: SyncTable, row_id: uuid.UUID, data: dict) -> None:
    model = table.model
    row = ctx.db.get(model, row_id, with_for_update=True)
    if row is not None and row.household_id not in ctx.household_ids:
        raise _Forbidden("row belongs to a household you're not in")
    data_household = _household_from(data)
    if row is not None and data_household is not None and data_household != row.household_id:
        raise _Invalid("rows can't move between households")

    if op == "DELETE":
        if row is not None and row.deleted_at is None:
            if table.on_delete is not None:
                table.on_delete(ctx, row)
            else:
                row.deleted_at = now()
        return

    if op == "PATCH" and row is None:
        raise _Invalid("row doesn't exist")

    changes = _changes(table, data)
    inserting = row is None
    if inserting:
        if data_household is None:
            raise _Invalid("household_id is required to create a row")
        if data_household not in ctx.household_ids:
            raise _Forbidden("household you're not in")
        row = model(id=row_id, household_id=data_household)
        ctx.db.add(row)
    elif op == "PUT":
        row.deleted_at = None
    for name, value in changes.items():
        setattr(row, name, value)
    if table.before_write is not None:
        table.before_write(ctx, row, op, inserting, changes)
    # On insert check every column (before_write may have filled some); else what changed.
    to_check = {c.name for c in model.__table__.columns} if inserting else set(changes)
    _check_references(ctx, table, row, to_check)


def apply_batch(db: Session, user: User, household_ids: set[uuid.UUID], entries: list) -> int:
    """Apply entries in order inside one transaction; commit on success."""
    ctx = WriteContext(db=db, user=user, household_ids=household_ids)
    try:
        for index, entry in enumerate(entries):
            table = get_table(entry.table)
            try:
                if table is None:
                    raise _Invalid(f"table {entry.table!r} can't be written by clients")
                try:
                    row_id = uuid.UUID(str(entry.id))
                except ValueError as exc:
                    raise _Invalid("id is not a UUID") from exc
                _apply(ctx, entry.op, table, row_id, entry.data or {})
                db.flush()
            except _Forbidden as exc:
                raise UploadError(403, index, entry, str(exc)) from exc
            except (_Invalid, WriteRejected) as exc:
                raise UploadError(422, index, entry, str(exc)) from exc
            except (IntegrityError, DataError) as exc:
                message = str(exc.orig).splitlines()[0] if exc.orig else "constraint violated"
                raise UploadError(422, index, entry, message) from exc
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return len(entries)
