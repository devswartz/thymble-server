"""Signed-in test members: a user, their household membership and an access token.

Household-scoped routes need a bearer token plus the household in `X-Thymble-Household`
(`thymble.auth.deps.get_current_household`). `create_member` writes the rows straight to
the database and mints a real access token, so API tests go through the production
dependency rather than overriding it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import sessionmaker

from tests.conftest import make_settings
from thymble.auth.deps import HOUSEHOLD_HEADER
from thymble.auth.tokens import create_access_token
from thymble.config import Settings
from thymble.models import Household, HouseholdMember, HouseholdRole, User

TEST_AUTH_SECRET = "test-auth-secret-0123456789abcdefghijklmnop"


def authed_settings(**overrides: Any) -> Settings:
    """Hermetic settings that can sign and check access tokens."""
    overrides.setdefault("auth_jwt_secret", TEST_AUTH_SECRET)
    return make_settings(**overrides)


@dataclass(frozen=True)
class Member:
    user_id: uuid.UUID
    household_id: uuid.UUID
    access_token: str

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            HOUSEHOLD_HEADER: str(self.household_id),
        }

    def headers_for(self, household_id: uuid.UUID) -> dict[str, str]:
        """This member's token, acting on another household (for isolation tests)."""
        return {**self.headers, HOUSEHOLD_HEADER: str(household_id)}


def create_member(
    sm: sessionmaker,
    settings: Settings,
    *,
    household_id: uuid.UUID | None = None,
    name: str = "Test cook",
    role: HouseholdRole = HouseholdRole.OWNER,
) -> Member:
    """A new user in `household_id` (or in a new household of their own)."""
    with sm() as session:
        user = User(display_name=name)
        session.add(user)
        if household_id is None:
            household = Household(name=f"{name}'s household")
            session.add(household)
            session.flush()
            household_id = household.id
        session.flush()
        session.add(HouseholdMember(household_id=household_id, user_id=user.id, role=role))
        session.commit()
        user_id = user.id
    token, _ = create_access_token(settings, user_id)
    return Member(user_id=user_id, household_id=household_id, access_token=token)
