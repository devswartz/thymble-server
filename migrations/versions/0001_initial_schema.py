"""initial schema

Core tables from architecture §4 and §6.4. Choices made where the doc was open:
- `user` is reserved in Postgres, so the table is `app_user`.
- Enums are VARCHAR + CHECK (not native Postgres enums): easier to extend, plain text on devices.
- Catalog rows (`ingredient`, `ingredient_alias`) have a nullable household_id:
  NULL = global/seeded, set = learned for one household.
- `app_user`, `household` and `llm_cache` carry no household_id; `llm_cache` is global,
  server-only and has no soft delete.
- `item_store_pref` is one row per (item, store or store group); `is_home` marks the home.
  No retailer product ids are stored (D-023 / O-7).
- Deferred: `unit`, `product`, `store_aisle_order` (units are text codes for now).

Revision ID: 0001
Revises:
Create Date: 2026-10-29 16:32:42.207410
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apple_sub", sa.String(length=255), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("apple_sub", name=op.f("uq_app_user_apple_sub")),
        sa.UniqueConstraint("email", name=op.f("uq_app_user_email")),
    )
    op.create_table(
        "household",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_household")),
    )
    op.create_table(
        "llm_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cache_key", sa.String(length=128), nullable=False),
        sa.Column("task", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_cache")),
        sa.UniqueConstraint("cache_key", name=op.f("uq_llm_cache_cache_key")),
    )
    op.create_index(op.f("ix_llm_cache_task"), "llm_cache", ["task"], unique=False)
    op.create_table(
        "connector_account",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("credentials_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_connector_account_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_account")),
    )
    op.create_index(
        op.f("ix_connector_account_household_id"),
        "connector_account",
        ["household_id"],
        unique=False,
    )
    op.create_table(
        "household_member",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "member",
                name="household_role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_household_member_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_household_member_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_household_member")),
        sa.UniqueConstraint(
            "household_id", "user_id", name=op.f("uq_household_member_household_id_user_id")
        ),
    )
    op.create_index(
        op.f("ix_household_member_household_id"), "household_member", ["household_id"], unique=False
    )
    op.create_index(
        op.f("ix_household_member_user_id"), "household_member", ["user_id"], unique=False
    )
    op.create_table(
        "ingredient",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("default_aisle_category", sa.String(length=64), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_ingredient_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingredient")),
    )
    op.create_index(
        op.f("ix_ingredient_household_id"), "ingredient", ["household_id"], unique=False
    )
    op.create_index(op.f("ix_ingredient_name"), "ingredient", ["name"], unique=False)
    op.create_table(
        "learning_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_learning_event_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_learning_event_user_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_event")),
    )
    op.create_index(
        op.f("ix_learning_event_event_type"), "learning_event", ["event_type"], unique=False
    )
    op.create_index(
        op.f("ix_learning_event_household_id"), "learning_event", ["household_id"], unique=False
    )
    op.create_table(
        "recipe",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column(
            "source_type",
            sa.Enum(
                "url",
                "photo",
                "text",
                "manual",
                name="recipe_source_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("yield_text", sa.Text(), nullable=True),
        sa.Column("yield_quantity", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("yield_unit", sa.String(length=64), nullable=True),
        sa.Column("prep_time_minutes", sa.Integer(), nullable=True),
        sa.Column("cook_time_minutes", sa.Integer(), nullable=True),
        sa.Column("total_time_minutes", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name=op.f("ck_recipe_rating_range")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_recipe_created_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_recipe_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe")),
    )
    op.create_index(op.f("ix_recipe_household_id"), "recipe", ["household_id"], unique=False)
    op.create_table(
        "shopping_list",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_shopping_list_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shopping_list")),
    )
    op.create_index(
        op.f("ix_shopping_list_household_id"), "shopping_list", ["household_id"], unique=False
    )
    op.create_table(
        "store_group",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_store_group_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_store_group")),
    )
    op.create_index(
        op.f("ix_store_group_household_id"), "store_group", ["household_id"], unique=False
    )
    op.create_table(
        "tag",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_tag_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tag")),
    )
    op.create_index(op.f("ix_tag_household_id"), "tag", ["household_id"], unique=False)
    op.create_table(
        "ingredient_alias",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=True),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_ingredient_alias_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredient.id"],
            name=op.f("fk_ingredient_alias_ingredient_id_ingredient"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingredient_alias")),
    )
    op.create_index(op.f("ix_ingredient_alias_alias"), "ingredient_alias", ["alias"], unique=False)
    op.create_index(
        op.f("ix_ingredient_alias_household_id"), "ingredient_alias", ["household_id"], unique=False
    )
    op.create_index(
        op.f("ix_ingredient_alias_ingredient_id"),
        "ingredient_alias",
        ["ingredient_id"],
        unique=False,
    )
    op.create_table(
        "meal_plan_entry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "meal_slot",
            sa.Enum(
                "breakfast",
                "lunch",
                "dinner",
                "snack",
                name="meal_slot",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("recipe_id", sa.Uuid(), nullable=True),
        sa.Column("free_text", sa.Text(), nullable=True),
        sa.Column("servings", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "recipe_id IS NOT NULL OR free_text IS NOT NULL",
            name=op.f("ck_meal_plan_entry_recipe_or_free_text"),
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_meal_plan_entry_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_meal_plan_entry_recipe_id_recipe"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meal_plan_entry")),
    )
    op.create_index(op.f("ix_meal_plan_entry_date"), "meal_plan_entry", ["date"], unique=False)
    op.create_index(
        op.f("ix_meal_plan_entry_household_id"), "meal_plan_entry", ["household_id"], unique=False
    )
    op.create_index(
        op.f("ix_meal_plan_entry_recipe_id"), "meal_plan_entry", ["recipe_id"], unique=False
    )
    op.create_table(
        "recipe_ingredient",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("quantity_max", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("ingredient_id", sa.Uuid(), nullable=True),
        sa.Column("preparation", sa.Text(), nullable=True),
        sa.Column("group_name", sa.Text(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_recipe_ingredient_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredient.id"],
            name=op.f("fk_recipe_ingredient_ingredient_id_ingredient"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_recipe_ingredient_recipe_id_recipe"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe_ingredient")),
    )
    op.create_index(
        op.f("ix_recipe_ingredient_household_id"),
        "recipe_ingredient",
        ["household_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recipe_ingredient_ingredient_id"),
        "recipe_ingredient",
        ["ingredient_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recipe_ingredient_recipe_id"), "recipe_ingredient", ["recipe_id"], unique=False
    )
    op.create_table(
        "recipe_step",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("group_name", sa.Text(), nullable=True),
        sa.Column("timers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_recipe_step_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_recipe_step_recipe_id_recipe"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe_step")),
    )
    op.create_index(
        op.f("ix_recipe_step_household_id"), "recipe_step", ["household_id"], unique=False
    )
    op.create_index(op.f("ix_recipe_step_recipe_id"), "recipe_step", ["recipe_id"], unique=False)
    op.create_table(
        "recipe_tag",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_recipe_tag_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_recipe_tag_recipe_id_recipe"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tag.id"], name=op.f("fk_recipe_tag_tag_id_tag"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe_tag")),
    )
    op.create_index(
        op.f("ix_recipe_tag_household_id"), "recipe_tag", ["household_id"], unique=False
    )
    op.create_index(op.f("ix_recipe_tag_recipe_id"), "recipe_tag", ["recipe_id"], unique=False)
    op.create_index(op.f("ix_recipe_tag_tag_id"), "recipe_tag", ["tag_id"], unique=False)
    op.create_table(
        "store",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("chain", sa.String(length=64), nullable=True),
        sa.Column("location_label", sa.Text(), nullable=True),
        sa.Column(
            "supports_in_store", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("supports_pickup", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "supports_delivery", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_bulk", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("external_list_connector_account_id", sa.Uuid(), nullable=True),
        sa.Column("external_list_id", sa.Text(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["external_list_connector_account_id"],
            ["connector_account.id"],
            name=op.f("fk_store_external_list_connector_account_id_connector_account"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_store_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_store")),
    )
    op.create_index(op.f("ix_store_household_id"), "store", ["household_id"], unique=False)
    op.create_table(
        "item_store_pref",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("store_group_id", sa.Uuid(), nullable=True),
        sa.Column("is_home", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("preferred_search_terms", sa.Text(), nullable=True),
        sa.Column("preferred_brand", sa.Text(), nullable=True),
        sa.Column("preferred_size", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("typical_quantity", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("typical_unit", sa.String(length=32), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "store_id IS NOT NULL OR store_group_id IS NOT NULL",
            name=op.f("ck_item_store_pref_store_or_group"),
        ),
        sa.CheckConstraint(
            "store_id IS NULL OR store_group_id IS NULL",
            name=op.f("ck_item_store_pref_store_xor_group"),
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_item_store_pref_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredient.id"],
            name=op.f("fk_item_store_pref_ingredient_id_ingredient"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["store_group_id"],
            ["store_group.id"],
            name=op.f("fk_item_store_pref_store_group_id_store_group"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["store.id"],
            name=op.f("fk_item_store_pref_store_id_store"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_store_pref")),
    )
    op.create_index(
        op.f("ix_item_store_pref_household_id"), "item_store_pref", ["household_id"], unique=False
    )
    op.create_index(
        op.f("ix_item_store_pref_ingredient_id"), "item_store_pref", ["ingredient_id"], unique=False
    )
    op.create_index(
        op.f("ix_item_store_pref_store_group_id"),
        "item_store_pref",
        ["store_group_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_item_store_pref_store_id"), "item_store_pref", ["store_id"], unique=False
    )
    op.create_table(
        "store_group_member",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("store_group_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_store_group_member_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["store_group_id"],
            ["store_group.id"],
            name=op.f("fk_store_group_member_store_group_id_store_group"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["store.id"],
            name=op.f("fk_store_group_member_store_id_store"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_store_group_member")),
    )
    op.create_index(
        op.f("ix_store_group_member_household_id"),
        "store_group_member",
        ["household_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_store_group_member_store_group_id"),
        "store_group_member",
        ["store_group_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_store_group_member_store_id"), "store_group_member", ["store_id"], unique=False
    )
    op.create_table(
        "trip",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "in_store",
                "pickup",
                "delivery",
                name="trip_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "planning",
                "on_order",
                "awaiting_confirmation",
                "done",
                "cancelled",
                name="trip_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("expected_pickup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_trip_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["store.id"], name=op.f("fk_trip_store_id_store"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_trip_user_id_app_user"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trip")),
    )
    op.create_index(op.f("ix_trip_household_id"), "trip", ["household_id"], unique=False)
    op.create_index(op.f("ix_trip_store_id"), "trip", ["store_id"], unique=False)
    op.create_index(op.f("ix_trip_user_id"), "trip", ["user_id"], unique=False)
    op.create_table(
        "shopping_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("list_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("display_text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "needed",
                "on_order",
                "bought",
                name="shopping_item_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_by", sa.Uuid(), nullable=True),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("store_group_id", sa.Uuid(), nullable=True),
        sa.Column(
            "urgency",
            sa.Enum(
                "normal",
                "urgent",
                name="shopping_item_urgency",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("bridge_of", sa.Uuid(), nullable=True),
        sa.Column("trip_id", sa.Uuid(), nullable=True),
        sa.Column("aisle", sa.String(length=64), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "store_id IS NULL OR store_group_id IS NULL",
            name=op.f("ck_shopping_item_store_xor_group"),
        ),
        sa.ForeignKeyConstraint(
            ["bridge_of"],
            ["shopping_item.id"],
            name=op.f("fk_shopping_item_bridge_of_shopping_item"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["checked_by"],
            ["app_user.id"],
            name=op.f("fk_shopping_item_checked_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_shopping_item_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredient.id"],
            name=op.f("fk_shopping_item_ingredient_id_ingredient"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["shopping_list.id"],
            name=op.f("fk_shopping_item_list_id_shopping_list"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["store_group_id"],
            ["store_group.id"],
            name=op.f("fk_shopping_item_store_group_id_store_group"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["store.id"],
            name=op.f("fk_shopping_item_store_id_store"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trip_id"],
            ["trip.id"],
            name=op.f("fk_shopping_item_trip_id_trip"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shopping_item")),
    )
    op.create_index(
        op.f("ix_shopping_item_bridge_of"), "shopping_item", ["bridge_of"], unique=False
    )
    op.create_index(
        op.f("ix_shopping_item_household_id"), "shopping_item", ["household_id"], unique=False
    )
    op.create_index(
        op.f("ix_shopping_item_ingredient_id"), "shopping_item", ["ingredient_id"], unique=False
    )
    op.create_index(op.f("ix_shopping_item_list_id"), "shopping_item", ["list_id"], unique=False)
    op.create_index(
        op.f("ix_shopping_item_store_group_id"), "shopping_item", ["store_group_id"], unique=False
    )
    op.create_index(op.f("ix_shopping_item_store_id"), "shopping_item", ["store_id"], unique=False)
    op.create_index(op.f("ix_shopping_item_trip_id"), "shopping_item", ["trip_id"], unique=False)
    op.create_table(
        "connector_item_link",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("connector_account_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_item_id", sa.Uuid(), nullable=False),
        sa.Column("external_list_id", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("last_synced_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["connector_account_id"],
            ["connector_account.id"],
            name=op.f("fk_connector_item_link_connector_account_id_connector_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_connector_item_link_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shopping_item_id"],
            ["shopping_item.id"],
            name=op.f("fk_connector_item_link_shopping_item_id_shopping_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_item_link")),
        sa.UniqueConstraint(
            "connector_account_id",
            "external_list_id",
            "external_id",
            name=op.f("uq_connector_item_link_connector_account_id_external_list_id_external_id"),
        ),
    )
    op.create_index(
        op.f("ix_connector_item_link_connector_account_id"),
        "connector_item_link",
        ["connector_account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_connector_item_link_household_id"),
        "connector_item_link",
        ["household_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_connector_item_link_shopping_item_id"),
        "connector_item_link",
        ["shopping_item_id"],
        unique=False,
    )
    op.create_table(
        "shopping_item_source",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_item_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum(
                "recipe",
                "meal_plan_entry",
                "manual",
                "connector",
                name="shopping_item_source_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("recipe_id", sa.Uuid(), nullable=True),
        sa.Column("meal_plan_entry_id", sa.Uuid(), nullable=True),
        sa.Column("connector_account_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["connector_account_id"],
            ["connector_account.id"],
            name=op.f("fk_shopping_item_source_connector_account_id_connector_account"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_shopping_item_source_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["meal_plan_entry_id"],
            ["meal_plan_entry.id"],
            name=op.f("fk_shopping_item_source_meal_plan_entry_id_meal_plan_entry"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_shopping_item_source_recipe_id_recipe"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shopping_item_id"],
            ["shopping_item.id"],
            name=op.f("fk_shopping_item_source_shopping_item_id_shopping_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shopping_item_source")),
    )
    op.create_index(
        op.f("ix_shopping_item_source_connector_account_id"),
        "shopping_item_source",
        ["connector_account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shopping_item_source_household_id"),
        "shopping_item_source",
        ["household_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shopping_item_source_meal_plan_entry_id"),
        "shopping_item_source",
        ["meal_plan_entry_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shopping_item_source_recipe_id"),
        "shopping_item_source",
        ["recipe_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shopping_item_source_shopping_item_id"),
        "shopping_item_source",
        ["shopping_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_shopping_item_source_shopping_item_id"), table_name="shopping_item_source"
    )
    op.drop_index(op.f("ix_shopping_item_source_recipe_id"), table_name="shopping_item_source")
    op.drop_index(
        op.f("ix_shopping_item_source_meal_plan_entry_id"), table_name="shopping_item_source"
    )
    op.drop_index(op.f("ix_shopping_item_source_household_id"), table_name="shopping_item_source")
    op.drop_index(
        op.f("ix_shopping_item_source_connector_account_id"), table_name="shopping_item_source"
    )
    op.drop_table("shopping_item_source")
    op.drop_index(op.f("ix_connector_item_link_shopping_item_id"), table_name="connector_item_link")
    op.drop_index(op.f("ix_connector_item_link_household_id"), table_name="connector_item_link")
    op.drop_index(
        op.f("ix_connector_item_link_connector_account_id"), table_name="connector_item_link"
    )
    op.drop_table("connector_item_link")
    op.drop_index(op.f("ix_shopping_item_trip_id"), table_name="shopping_item")
    op.drop_index(op.f("ix_shopping_item_store_id"), table_name="shopping_item")
    op.drop_index(op.f("ix_shopping_item_store_group_id"), table_name="shopping_item")
    op.drop_index(op.f("ix_shopping_item_list_id"), table_name="shopping_item")
    op.drop_index(op.f("ix_shopping_item_ingredient_id"), table_name="shopping_item")
    op.drop_index(op.f("ix_shopping_item_household_id"), table_name="shopping_item")
    op.drop_index(op.f("ix_shopping_item_bridge_of"), table_name="shopping_item")
    op.drop_table("shopping_item")
    op.drop_index(op.f("ix_trip_user_id"), table_name="trip")
    op.drop_index(op.f("ix_trip_store_id"), table_name="trip")
    op.drop_index(op.f("ix_trip_household_id"), table_name="trip")
    op.drop_table("trip")
    op.drop_index(op.f("ix_store_group_member_store_id"), table_name="store_group_member")
    op.drop_index(op.f("ix_store_group_member_store_group_id"), table_name="store_group_member")
    op.drop_index(op.f("ix_store_group_member_household_id"), table_name="store_group_member")
    op.drop_table("store_group_member")
    op.drop_index(op.f("ix_item_store_pref_store_id"), table_name="item_store_pref")
    op.drop_index(op.f("ix_item_store_pref_store_group_id"), table_name="item_store_pref")
    op.drop_index(op.f("ix_item_store_pref_ingredient_id"), table_name="item_store_pref")
    op.drop_index(op.f("ix_item_store_pref_household_id"), table_name="item_store_pref")
    op.drop_table("item_store_pref")
    op.drop_index(op.f("ix_store_household_id"), table_name="store")
    op.drop_table("store")
    op.drop_index(op.f("ix_recipe_tag_tag_id"), table_name="recipe_tag")
    op.drop_index(op.f("ix_recipe_tag_recipe_id"), table_name="recipe_tag")
    op.drop_index(op.f("ix_recipe_tag_household_id"), table_name="recipe_tag")
    op.drop_table("recipe_tag")
    op.drop_index(op.f("ix_recipe_step_recipe_id"), table_name="recipe_step")
    op.drop_index(op.f("ix_recipe_step_household_id"), table_name="recipe_step")
    op.drop_table("recipe_step")
    op.drop_index(op.f("ix_recipe_ingredient_recipe_id"), table_name="recipe_ingredient")
    op.drop_index(op.f("ix_recipe_ingredient_ingredient_id"), table_name="recipe_ingredient")
    op.drop_index(op.f("ix_recipe_ingredient_household_id"), table_name="recipe_ingredient")
    op.drop_table("recipe_ingredient")
    op.drop_index(op.f("ix_meal_plan_entry_recipe_id"), table_name="meal_plan_entry")
    op.drop_index(op.f("ix_meal_plan_entry_household_id"), table_name="meal_plan_entry")
    op.drop_index(op.f("ix_meal_plan_entry_date"), table_name="meal_plan_entry")
    op.drop_table("meal_plan_entry")
    op.drop_index(op.f("ix_ingredient_alias_ingredient_id"), table_name="ingredient_alias")
    op.drop_index(op.f("ix_ingredient_alias_household_id"), table_name="ingredient_alias")
    op.drop_index(op.f("ix_ingredient_alias_alias"), table_name="ingredient_alias")
    op.drop_table("ingredient_alias")
    op.drop_index(op.f("ix_tag_household_id"), table_name="tag")
    op.drop_table("tag")
    op.drop_index(op.f("ix_store_group_household_id"), table_name="store_group")
    op.drop_table("store_group")
    op.drop_index(op.f("ix_shopping_list_household_id"), table_name="shopping_list")
    op.drop_table("shopping_list")
    op.drop_index(op.f("ix_recipe_household_id"), table_name="recipe")
    op.drop_table("recipe")
    op.drop_index(op.f("ix_learning_event_household_id"), table_name="learning_event")
    op.drop_index(op.f("ix_learning_event_event_type"), table_name="learning_event")
    op.drop_table("learning_event")
    op.drop_index(op.f("ix_ingredient_name"), table_name="ingredient")
    op.drop_index(op.f("ix_ingredient_household_id"), table_name="ingredient")
    op.drop_table("ingredient")
    op.drop_index(op.f("ix_household_member_user_id"), table_name="household_member")
    op.drop_index(op.f("ix_household_member_household_id"), table_name="household_member")
    op.drop_table("household_member")
    op.drop_index(op.f("ix_connector_account_household_id"), table_name="connector_account")
    op.drop_table("connector_account")
    op.drop_index(op.f("ix_llm_cache_task"), table_name="llm_cache")
    op.drop_table("llm_cache")
    op.drop_table("household")
    op.drop_table("app_user")
