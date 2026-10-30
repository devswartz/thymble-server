"""merge: canonical unit codes and import review

Joins the two migrations that were written side by side on top of shop_0001.
No schema change.

Revision ID: merge_0001
Revises: plan_0001, import_0002
Create Date: 2026-10-29 23:00:00
"""

from collections.abc import Sequence

revision: str = "merge_0001"
down_revision: str | tuple[str, ...] | None = ("plan_0001", "import_0002")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
