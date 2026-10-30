"""Recipe import jobs and LLM usage metering (architecture §5, §10.2).

Both tables are server-side only: they are not part of the PowerSync sync config.
Import jobs hold the raw input (page HTML, pasted text, photo) until the worker is done;
the draft recipe they produce is an ordinary synced `recipe` row with `is_draft = true`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, IdMixin, TimestampMixin, str_enum


class ImportStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ImportSourceKind(enum.StrEnum):
    HTML = "html"  # page HTML captured on the device (D-025)
    URL = "url"  # server-side fetch fallback
    TEXT = "text"
    PHOTO = "photo"


class ImportJob(IdMixin, TimestampMixin, Base):
    __tablename__ = "import_job"

    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), index=True, sort_order=-90
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    status: Mapped[ImportStatus] = mapped_column(
        str_enum(ImportStatus, "import_status"), default=ImportStatus.QUEUED
    )
    source_kind: Mapped[ImportSourceKind] = mapped_column(
        str_enum(ImportSourceKind, "import_source_kind")
    )
    # URL the user asked for (url imports) and the URL the page finally came from.
    requested_url: Mapped[str | None] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    # Raw input: {"html": ...} or {"text": ...}. Kept so an import can be re-run.
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Photo bytes; cleared once the worker has finished with them.
    photo: Mapped[bytes | None] = mapped_column(LargeBinary)
    photo_mime: Mapped[str | None] = mapped_column(String(64))
    # Pipeline output (parsed lines with confidence, flags, review reasons ...).
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    error: Mapped[str | None] = mapped_column(Text)
    recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipe.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LlmUsage(IdMixin, TimestampMixin, Base):
    """One LLM call (or cache hit), for per-household metering and spend limits."""

    __tablename__ = "llm_usage"

    household_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), index=True, sort_order=-90
    )
    task: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    cached: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    # ok | invalid (schema validation failed) | error | timeout
    status: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Estimated from the provider's list price; NULL for local models.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
