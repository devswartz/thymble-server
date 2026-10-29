"""PowerSync endpoints: the sync token (read path) and the upload endpoint (write path)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from thymble.auth.deps import AppSettings, CurrentUser, DbSession, member_household_ids
from thymble.auth.tokens import create_powersync_token
from thymble.sync import registered_tables
from thymble.sync.upload import UploadError, apply_batch

router = APIRouter(prefix="/sync", tags=["sync"])

MAX_BATCH = 1000


class SyncCredentials(BaseModel):
    """What the PowerSync client's fetchCredentials() returns."""

    endpoint: str
    token: str
    expires_at: datetime


@router.get("/token")
def sync_token(user: CurrentUser, settings: AppSettings) -> SyncCredentials:
    """A short-lived JWT for the PowerSync service (`aud: thymble-powersync`, `sub`: user id).

    PowerSync's sync config then limits the user to their households' rows.
    """
    token, expires = create_powersync_token(settings, user.id)
    return SyncCredentials(endpoint=settings.powersync_url, token=token, expires_at=expires)


class CrudEntry(BaseModel):
    """One queued client write, as the PowerSync SDKs describe it."""

    model_config = ConfigDict(extra="ignore")

    op: Literal["PUT", "PATCH", "DELETE"]
    table: str = Field(validation_alias=AliasChoices("table", "type"), max_length=64)
    id: str = Field(max_length=64)
    data: dict[str, Any] | None = Field(
        default=None, validation_alias=AliasChoices("data", "opData", "op_data")
    )


class UploadBatch(BaseModel):
    batch: list[CrudEntry] = Field(max_length=MAX_BATCH)


class UploadResult(BaseModel):
    applied: int


class WritableTable(BaseModel):
    table: str
    columns: list[str]


@router.post("/upload")
def upload(body: UploadBatch, user: CurrentUser, db: DbSession) -> UploadResult:
    """Apply a batch of client changes in one transaction (all or nothing).

    Every row must belong to one of the user's households, only registered tables and
    columns are accepted, and foreign keys must stay inside the household. On a 4xx the
    detail says which entry failed; nothing from the batch was written.
    """
    households = member_household_ids(db, user.id)
    try:
        applied = apply_batch(db, user, households, body.batch)
    except UploadError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return UploadResult(applied=applied)


@router.get("/tables")
def writable_tables(user: CurrentUser) -> list[WritableTable]:
    """Tables and columns the upload endpoint accepts (for client developers)."""
    return [
        WritableTable(table=name, columns=sorted(table.writable))
        for name, table in sorted(registered_tables().items())
    ]
