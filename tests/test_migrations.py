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


def test_plan_0001_rewrites_unit_codes(db_url):
    """Rows written with the importer's and the shopping list's old codes get one code."""
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")  # the module's database may already be at head
    command.upgrade(cfg, "shop_0001")
    engine = create_engine(db_url)
    ids = {name: uuid.uuid4() for name in ("hh", "recipe", "list", "ing", "group")}
    lines = {"fl oz": uuid.uuid4(), "piece": uuid.uuid4(), "cup": uuid.uuid4()}
    items = {
        "pint": uuid.uuid4(),
        "quart": uuid.uuid4(),
        "fl_oz": uuid.uuid4(),
        "ct": uuid.uuid4(),
        "pack": uuid.uuid4(),
        "boxe": uuid.uuid4(),
    }

    def units(table: str, column: str = "unit") -> dict[uuid.UUID, str]:
        with engine.connect() as conn:
            rows = conn.execute(text(f'SELECT id, "{column}" FROM "{table}"'))
            return {row[0]: row[1] for row in rows}

    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO household (id, name) VALUES (:hh, 'H')"), {"hh": ids["hh"]}
            )
            conn.execute(
                text(
                    "INSERT INTO recipe (id, household_id, title, source_type) "
                    "VALUES (:id, :hh, 'R', 'manual')"
                ),
                {"id": ids["recipe"], "hh": ids["hh"]},
            )
            for pos, (unit, line_id) in enumerate(lines.items()):
                conn.execute(
                    text(
                        "INSERT INTO recipe_ingredient "
                        "(id, household_id, recipe_id, position, raw_text, unit) "
                        "VALUES (:id, :hh, :r, :pos, 'x', :unit)"
                    ),
                    {"id": line_id, "hh": ids["hh"], "r": ids["recipe"], "pos": pos, "unit": unit},
                )
            conn.execute(
                text("INSERT INTO shopping_list (id, household_id, name) VALUES (:id, :hh, 'L')"),
                {"id": ids["list"], "hh": ids["hh"]},
            )
            for unit, item_id in items.items():
                conn.execute(
                    text(
                        "INSERT INTO shopping_item (id, household_id, list_id, display_text, "
                        "unit, status, urgency) VALUES (:id, :hh, :l, 'x', :unit, 'needed', "
                        "'normal')"
                    ),
                    {"id": item_id, "hh": ids["hh"], "l": ids["list"], "unit": unit},
                )
                conn.execute(
                    text(
                        "INSERT INTO shopping_item_source (id, household_id, shopping_item_id, "
                        "source_type, unit) VALUES (:id, :hh, :item, 'manual', :unit)"
                    ),
                    {"id": item_id, "hh": ids["hh"], "item": item_id, "unit": unit},
                )
            conn.execute(
                text("INSERT INTO ingredient (id, name) VALUES (:id, 'milk')"), {"id": ids["ing"]}
            )
            conn.execute(
                text("INSERT INTO store_group (id, household_id, name) VALUES (:id, :hh, 'G')"),
                {"id": ids["group"], "hh": ids["hh"]},
            )
            conn.execute(
                text(
                    "INSERT INTO item_store_pref (id, household_id, ingredient_id, "
                    "store_group_id, typical_unit) VALUES (:id, :hh, :ing, :g, 'quart')"
                ),
                {"id": ids["ing"], "hh": ids["hh"], "ing": ids["ing"], "g": ids["group"]},
            )

        command.upgrade(cfg, "plan_0001")
        # Recipe lines already used the canonical (importer) codes.
        assert units("recipe_ingredient") == {line_id: u for u, line_id in lines.items()}
        expected_items = {
            items["pint"]: "pt",
            items["quart"]: "qt",
            items["fl_oz"]: "fl oz",
            items["ct"]: "piece",
            items["pack"]: "package",
            items["boxe"]: "box",
        }
        assert units("shopping_item") == expected_items
        assert units("shopping_item_source") == expected_items
        assert units("item_store_pref", "typical_unit") == {ids["ing"]: "qt"}

        command.downgrade(cfg, "shop_0001")
        assert units("recipe_ingredient")[lines["fl oz"]] == "fl oz"
        assert units("recipe_ingredient")[lines["piece"]] == "piece"
        assert units("shopping_item")[items["pint"]] == "pint"
        assert units("shopping_item")[items["fl_oz"]] == "fl_oz"
        assert units("shopping_item")[items["ct"]] == "ct"
        assert units("item_store_pref", "typical_unit") == {ids["ing"]: "quart"}
    finally:
        engine.dispose()
