"""Request dependencies: the signed-in user and household membership checks.

Every route that touches household data must go through `require_member` (or, for bulk
work such as the sync upload, `member_household_ids`). Household is the data boundary
everywhere (architecture §12).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from thymble.auth.tokens import decode_access_token
from thymble.config import Settings, get_settings
from thymble.db.deps import get_db
from thymble.models import Household, HouseholdMember, HouseholdRole, User

_bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _unauthorized(detail: str = "not authenticated") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: DbSession,
    settings: AppSettings,
) -> User:
    if credentials is None:
        raise _unauthorized()
    try:
        claims = decode_access_token(settings, credentials.credentials)
        user_id = uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise _unauthorized("invalid or expired access token") from exc
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise _unauthorized("invalid or expired access token")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def member_household_ids(db: Session, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Households the user can currently read and write."""
    return set(
        db.scalars(
            select(HouseholdMember.household_id)
            .join(Household, Household.id == HouseholdMember.household_id)
            .where(
                HouseholdMember.user_id == user_id,
                HouseholdMember.deleted_at.is_(None),
                Household.deleted_at.is_(None),
            )
        )
    )


def require_member(
    db: Session, user: User, household_id: uuid.UUID, role: HouseholdRole | None = None
) -> HouseholdMember:
    """The user's membership, or 404 (non-members can't tell the household exists).

    With `role=OWNER`, members who aren't owners get 403.
    """
    membership = db.scalars(
        select(HouseholdMember)
        .join(Household, Household.id == HouseholdMember.household_id)
        .where(
            HouseholdMember.household_id == household_id,
            HouseholdMember.user_id == user.id,
            HouseholdMember.deleted_at.is_(None),
            Household.deleted_at.is_(None),
        )
    ).one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "household not found")
    if role is HouseholdRole.OWNER and membership.role is not HouseholdRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only household owners can do that")
    return membership


HOUSEHOLD_HEADER = "X-Thymble-Household"


@dataclass(frozen=True)
class CurrentHousehold:
    """The household a request acts on, and the signed-in member acting on it."""

    household_id: uuid.UUID
    user_id: uuid.UUID
    role: HouseholdRole


def get_current_household(
    user: CurrentUser,
    db: DbSession,
    x_thymble_household: Annotated[
        str | None, Header(description="Household the request acts on (UUID)")
    ] = None,
) -> CurrentHousehold:
    """The household named in `X-Thymble-Household`, after checking the user belongs to it."""
    if not x_thymble_household:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{HOUSEHOLD_HEADER} header required")
    try:
        household_id = uuid.UUID(x_thymble_household)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{HOUSEHOLD_HEADER} must be a UUID"
        ) from exc
    membership = require_member(db, user, household_id)
    return CurrentHousehold(household_id, user.id, membership.role)


CurrentHouseholdDep = Annotated[CurrentHousehold, Depends(get_current_household)]
