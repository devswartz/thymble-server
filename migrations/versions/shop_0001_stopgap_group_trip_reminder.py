"""shopping: default store group for stop-gaps, trip reminder timestamp

- `store_group.is_default`: the household's everyday group; stop-gap items (D-029) are
  assigned to it when the bulk item has no non-bulk "also OK" store.
- `trip.reminded_at`: when the last "Got everything?" reminder went out (D-030), so the
  reminder job does not repeat itself.

Revision ID: shop_0001
Revises: 0001
Create Date: 2026-10-29 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "shop_0001"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "store_group",
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("trip", sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("trip", "reminded_at")
    op.drop_column("store_group", "is_default")
