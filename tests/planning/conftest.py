"""Fixtures for recipe and meal plan API tests: the shopping suite's throwaway database,
plus a signed-in member and a REST client for that member's household.

Every recipe and line here is synthetic, written for these tests.
"""

from __future__ import annotations

import pytest

from tests.accounts import Member, authed_settings, create_member
from tests.shopping.conftest import (  # noqa: F401 - re-exported fixtures
    db,
    household,
    sessions,
    setup,
    shop_db_url,
)
from tests.shopping.test_api import make_client


@pytest.fixture
def member(sessions, household) -> Member:  # noqa: F811
    return create_member(sessions, authed_settings(), household_id=household)


@pytest.fixture
def api(sessions, member):  # noqa: F811
    return make_client(sessions, member)


@pytest.fixture
def stranger_api(sessions):  # noqa: F811
    """A member of another household."""
    return make_client(sessions, create_member(sessions, authed_settings(), name="Stranger"))
