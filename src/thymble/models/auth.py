"""Server-side auth state: refresh tokens, email sign-in tokens, household invites (§9).

None of these tables are synced to devices: they hold token hashes. Tokens themselves
are never stored, only their SHA-256 hashes, so a database leak can't be replayed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, IdMixin


class RefreshToken(IdMixin, Base):
    """One refresh token. Rotation issues a new row in the same `family_id`.

    Presenting a token that was already rotated (reuse) revokes the whole family, which
    logs out a stolen copy and the legitimate client alike.
    """

    __tablename__ = "auth_refresh_token"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # OAuth2 client the token was issued to: "thymble-app" today; later e.g. "alexa"
    # when the authorization-code endpoint for account linking exists.
    client_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Set when this token was exchanged for a new one (rotation) or revoked (logout/reuse).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailLoginToken(IdMixin, Base):
    """A magic-link token: single use (`used_at`), short-lived (`expires_at`)."""

    __tablename__ = "auth_email_token"

    # Lower-cased address the link was sent to.
    email: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # SHA-256 of the requester's IP, only for rate limiting (no raw IPs stored).
    requester_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HouseholdInvite(IdMixin, Base):
    """A single-use code that lets one person join a household."""

    __tablename__ = "household_invite"

    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
