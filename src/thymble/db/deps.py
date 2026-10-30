"""FastAPI dependency that hands each request its own database session."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from thymble.db.session import get_sessionmaker


def get_db() -> Iterator[Session]:
    """One session per request. Routes commit explicitly; anything uncommitted rolls back."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
