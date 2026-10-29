"""Migrations apply cleanly to a real PostgreSQL server and match the models.

Needs DATABASE_URL pointing at a Postgres server where the user may create databases.
Each run uses a throwaway database, so it never touches the app's own data.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from thymble.config import normalize_db_url
from thymble.models import Base

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "app_user",
    "household",
    "household_member",
    "recipe",
    "recipe_ingredient",
    "recipe_step",
    "tag",
    "recipe_tag",
    "ingredient",
    "ingredient_alias",
    "meal_plan_entry",
    "shopping_list",
    "shopping_item",
    "shopping_item_source",
    "trip",
    "store",
    "store_group",
    "store_group_member",
    "item_store_pref",
    "connector_account",
    "connector_item_link",
    "learning_event",
    "llm_cache",
}
GLOBAL_TABLES = {"app_user", "household", "llm_cache"}


@pytest.fixture(scope="module")
def db_url() -> Iterator[str]:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        pytest.skip("DATABASE_URL not set")
    admin_url = make_url(normalize_db_url(raw))
    name = f"thymble_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield admin_url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logging"] = False
    return cfg


def test_upgrade_downgrade_upgrade(db_url):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert tables >= EXPECTED_TABLES

        command.downgrade(cfg, "base")
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}

        command.upgrade(cfg, "head")
        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()


def test_models_match_migrations(db_url):
    command.upgrade(alembic_config(db_url), "head")
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert diff == [], f"models and migrations differ; run alembic revision --autogenerate: {diff}"


def test_synced_tables_carry_sync_columns(db_url):
    command.upgrade(alembic_config(db_url), "head")
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        for table in EXPECTED_TABLES:
            columns = {c["name"]: c for c in insp.get_columns(table)}
            assert str(columns["id"]["type"]) == "UUID", table
            assert {"created_at", "updated_at"} <= columns.keys(), table
            if table not in GLOBAL_TABLES:
                assert "household_id" in columns, table
            if table != "llm_cache":
                assert "deleted_at" in columns, table
    finally:
        engine.dispose()
