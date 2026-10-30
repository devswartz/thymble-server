"""import review: recipe review state, per-line and per-step confidence (D-034, D-035)

- `recipe.review_state` (needs_review | reviewed; NULL for recipes typed in) and
  `recipe.review` (JSONB: section confidence for title, yield, times, groups and
  completeness, the import's counts, and who reviewed it when).
- `recipe_ingredient.confidence` / `recipe_step.confidence` (high | check | low; NULL for
  rows typed in) and `.review` (JSONB: reasons, reason codes, the two photo readings, the
  review outcome).

The level is a real column so devices and the server can filter and count on it ("2 lines
need a look", "can this be finalized?"); the reasons are JSONB because they are a
variable-length list that is only displayed, and they will grow (readings, outcomes).
All are server-owned: the sync upload treats them as read-only.

Revision ID: import_0002
Revises: shop_0001
Create Date: 2026-10-29 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "import_0002"
down_revision: str | None = "shop_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _confidence() -> sa.Enum:
    return sa.Enum(
        "high",
        "check",
        "low",
        name="import_confidence",
        native_enum=False,
        create_constraint=True,
        length=32,
    )


def upgrade() -> None:
    op.add_column(
        "recipe",
        sa.Column(
            "review_state",
            sa.Enum(
                "needs_review",
                "reviewed",
                name="recipe_review_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "recipe", sa.Column("review", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    for table in ("recipe_ingredient", "recipe_step"):
        op.add_column(table, sa.Column("confidence", _confidence(), nullable=True))
        op.add_column(
            table, sa.Column("review", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
        )


def downgrade() -> None:
    for table in ("recipe_step", "recipe_ingredient"):
        op.drop_column(table, "review")
        op.drop_column(table, "confidence")
    op.drop_column("recipe", "review")
    op.drop_column("recipe", "review_state")
