"""import jobs, llm usage, draft recipes

Recipe import pipeline and LLM module (architecture §5, §10.2):
- `import_job`: one row per import request (device HTML, URL, text or photo), its status,
  the raw input, the pipeline result and the draft recipe it produced. Server-side only.
- `llm_usage`: one row per LLM call or cache hit, for per-household metering and spend caps.
  Server-side only.
- `recipe.is_draft`: true while an imported recipe waits for the user's review.

Revision ID: import_0001
Revises: auth_0002
Create Date: 2026-10-29 11:51:21.298206
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "import_0001"
down_revision: str | None = "auth_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("task", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("cached", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_llm_usage_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_usage")),
    )
    op.create_index(op.f("ix_llm_usage_household_id"), "llm_usage", ["household_id"], unique=False)
    op.create_index(op.f("ix_llm_usage_task"), "llm_usage", ["task"], unique=False)
    op.create_table(
        "import_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "done",
                "failed",
                name="import_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "source_kind",
            sa.Enum(
                "html",
                "url",
                "text",
                "photo",
                name="import_source_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("requested_url", sa.Text(), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("photo", sa.LargeBinary(), nullable=True),
        sa.Column("photo_mime", sa.String(length=64), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("needs_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("recipe_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_import_job_created_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_import_job_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_import_job_recipe_id_recipe"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_job")),
    )
    op.create_index(
        op.f("ix_import_job_household_id"), "import_job", ["household_id"], unique=False
    )
    op.create_index(op.f("ix_import_job_recipe_id"), "import_job", ["recipe_id"], unique=False)
    op.add_column(
        "recipe",
        sa.Column("is_draft", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("recipe", "is_draft")
    op.drop_index(op.f("ix_import_job_recipe_id"), table_name="import_job")
    op.drop_index(op.f("ix_import_job_household_id"), table_name="import_job")
    op.drop_table("import_job")
    op.drop_index(op.f("ix_llm_usage_task"), table_name="llm_usage")
    op.drop_index(op.f("ix_llm_usage_household_id"), table_name="llm_usage")
    op.drop_table("llm_usage")
