"""auth tokens and household invites

Server-only tables for accounts and households (architecture §9, D-015):
- `auth_refresh_token`: hashed, rotating refresh tokens grouped in families (reuse revokes
  the family); `client_id` leaves room for OAuth2 clients such as Alexa account linking.
- `auth_email_token`: hashed single-use magic-link tokens; also the rate-limit ledger.
- `household_invite`: hashed single-use invite codes.
None of these are synced to devices.

Revision ID: auth_0002
Revises: 0001
Create Date: 2026-10-29 16:51:15.513716
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "auth_0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_email_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("requester_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_email_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_email_token_token_hash")),
    )
    op.create_index(
        op.f("ix_auth_email_token_created_at"), "auth_email_token", ["created_at"], unique=False
    )
    op.create_index(op.f("ix_auth_email_token_email"), "auth_email_token", ["email"], unique=False)
    op.create_index(
        op.f("ix_auth_email_token_requester_hash"),
        "auth_email_token",
        ["requester_hash"],
        unique=False,
    )
    op.create_table(
        "auth_refresh_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_auth_refresh_token_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_refresh_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_refresh_token_token_hash")),
    )
    op.create_index(
        op.f("ix_auth_refresh_token_family_id"), "auth_refresh_token", ["family_id"], unique=False
    )
    op.create_index(
        op.f("ix_auth_refresh_token_user_id"), "auth_refresh_token", ["user_id"], unique=False
    )
    op.create_table(
        "household_invite",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_household_invite_created_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_household_invite_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["used_by"],
            ["app_user.id"],
            name=op.f("fk_household_invite_used_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_household_invite")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_household_invite_code_hash")),
    )
    op.create_index(
        op.f("ix_household_invite_household_id"), "household_invite", ["household_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_household_invite_household_id"), table_name="household_invite")
    op.drop_table("household_invite")
    op.drop_index(op.f("ix_auth_refresh_token_user_id"), table_name="auth_refresh_token")
    op.drop_index(op.f("ix_auth_refresh_token_family_id"), table_name="auth_refresh_token")
    op.drop_table("auth_refresh_token")
    op.drop_index(op.f("ix_auth_email_token_requester_hash"), table_name="auth_email_token")
    op.drop_index(op.f("ix_auth_email_token_email"), table_name="auth_email_token")
    op.drop_index(op.f("ix_auth_email_token_created_at"), table_name="auth_email_token")
    op.drop_table("auth_email_token")
