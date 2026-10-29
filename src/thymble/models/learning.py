"""Learning layer: append-only events and the LLM result cache (architecture §4, §7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, IdMixin, SyncedBase, TimestampMixin


class LearningEvent(SyncedBase, Base):
    """A correction or choice (parse fixed, alias confirmed, item checked at store X ...).

    Append-only by convention: rows are never updated; aggregates are rebuilt by worker jobs.
    """

    __tablename__ = "learning_event"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    # e.g. "parse_corrected", "alias_confirmed", "item_checked"
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[uuid.UUID | None] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LlmCache(IdMixin, TimestampMixin, Base):
    """Normalized prompt key -> result. Global (not household-scoped) and never synced."""

    __tablename__ = "llm_cache"

    # Hash of task + normalized input + output schema version.
    cache_key: Mapped[str] = mapped_column(String(128), unique=True)
    task: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    hit_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
