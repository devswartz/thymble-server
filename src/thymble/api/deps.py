"""Shared request dependencies: database session and the caller's household.

`get_current_household` is a stand-in until auth lands: it is the one place to swap for
the real token check. Until then it only works in development (THYMBLE_ENV=development),
where it reads the household from the `X-Thymble-Household` header (and optionally the
user from `X-Thymble-User`). Tests override it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from thymble.config import Settings, get_settings
from thymble.db.session import get_sessionmaker


@dataclass(frozen=True)
class CurrentHousehold:
    household_id: uuid.UUID
    user_id: uuid.UUID | None = None


def get_db() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def get_current_household(
    settings: Annotated[Settings, Depends(get_settings)],
    x_thymble_household: Annotated[uuid.UUID | None, Header()] = None,
    x_thymble_user: Annotated[uuid.UUID | None, Header()] = None,
) -> CurrentHousehold:
    if settings.thymble_env != "development" or x_thymble_household is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    return CurrentHousehold(x_thymble_household, x_thymble_user)


DbSession = Annotated[Session, Depends(get_db)]
Household = Annotated[CurrentHousehold, Depends(get_current_household)]
