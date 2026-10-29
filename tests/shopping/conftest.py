"""Fixtures for shopping / connector tests.

Database tests need DATABASE_URL (a server where the user may create databases). One
throwaway database is migrated per test session; tables are emptied after each test.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from thymble.config import normalize_db_url
from thymble.models import Base, Household, ShoppingList, Store, StoreGroup, StoreGroupMember
from thymble.shopping.service import ShoppingService

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def shop_db_url() -> Iterator[str]:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        pytest.skip("DATABASE_URL not set")
    admin_url = make_url(normalize_db_url(raw))
    name = f"thymble_shop_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logging"] = False
    command.upgrade(cfg, "head")
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def sessions(shop_db_url) -> Iterator[sessionmaker]:
    engine = create_engine(shop_db_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {tables} CASCADE"))
        engine.dispose()


@pytest.fixture
def db(sessions) -> Iterator[Session]:
    with sessions() as session:
        yield session


@pytest.fixture
def household(sessions) -> uuid.UUID:
    with sessions() as s:
        hh = Household(name="Test household")
        s.add(hh)
        s.commit()
        return hh.id


@pytest.fixture
def svc(db, household) -> ShoppingService:
    return ShoppingService(db, household)


class Setup:
    """The founder's setup (D-027): Aldi + Walmart = Everyday grocery, Costco bulk."""

    def __init__(self, s: Session, hh: uuid.UUID):
        self.list = ShoppingList(household_id=hh, name="Shopping")
        self.aldi = Store(household_id=hh, name="Aldi", chain="aldi")
        self.walmart = Store(household_id=hh, name="Walmart", chain="walmart", supports_pickup=True)
        self.costco = Store(household_id=hh, name="Costco", chain="costco", is_bulk=True)
        self.everyday = StoreGroup(household_id=hh, name="Everyday grocery", is_default=True)
        s.add_all([self.list, self.aldi, self.walmart, self.costco, self.everyday])
        s.flush()
        for store in (self.aldi, self.walmart):
            s.add(
                StoreGroupMember(
                    household_id=hh, store_group_id=self.everyday.id, store_id=store.id
                )
            )
        s.commit()


@pytest.fixture
def setup(db, household) -> Setup:
    return Setup(db, household)
