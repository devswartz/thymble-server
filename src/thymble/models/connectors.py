"""Connector accounts and item links (architecture §4, §6.2, §6.3).

These tables are server-side only: they are not part of the PowerSync sync config,
because credentials must never reach devices.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, SyncedBase


class ConnectorAccount(SyncedBase, Base):
    __tablename__ = "connector_account"

    # Connector id, e.g. "home_assistant", "walmart", "kroger".
    kind: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    # Encrypted at rest by the api (security baseline §12); never synced.
    credentials_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Non-secret settings, e.g. which todo entity mirrors the list.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="active")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ConnectorItemLink(SyncedBase, Base):
    """shopping_item <-> external item. One item may have several links (e.g. Alexa duplicates)."""

    __tablename__ = "connector_item_link"
    __table_args__ = (UniqueConstraint("connector_account_id", "external_list_id", "external_id"),)

    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connector_account.id", ondelete="CASCADE"), index=True
    )
    shopping_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shopping_item.id", ondelete="CASCADE"), index=True
    )
    external_list_id: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    # Last state Thymble wrote or saw, for diffing and echo-loop suppression.
    last_synced_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
