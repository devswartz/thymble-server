"""Finding and creating users; every user gets a household on first sign-in."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from thymble.auth.apple import AppleIdentity
from thymble.auth.tokens import now
from thymble.models import Household, HouseholdMember, HouseholdRole, User

# Deliberately loose: one @, no spaces, a dot in the domain. Delivery is the real test.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 320 or not _EMAIL_RE.match(email):
        raise ValueError("not a valid email address")
    return email


def active_memberships(db: Session, user_id: uuid.UUID) -> list[HouseholdMember]:
    return list(
        db.scalars(
            select(HouseholdMember)
            .join(Household, Household.id == HouseholdMember.household_id)
            .where(
                HouseholdMember.user_id == user_id,
                HouseholdMember.deleted_at.is_(None),
                Household.deleted_at.is_(None),
            )
            .order_by(HouseholdMember.created_at)
        )
    )


def create_household(db: Session, user: User, name: str) -> Household:
    household = Household(id=uuid.uuid4(), name=name)
    db.add(household)
    db.flush()
    db.add(HouseholdMember(household_id=household.id, user_id=user.id, role=HouseholdRole.OWNER))
    db.flush()
    return household


def ensure_household(db: Session, user: User) -> None:
    """Create a household (owned by the user) if they don't belong to any."""
    if not active_memberships(db, user.id):
        name = f"{user.display_name}'s household" if user.display_name else "My household"
        create_household(db, user, name)


def _active_user(db: Session, *where) -> User | None:
    return db.scalars(select(User).where(*where, User.deleted_at.is_(None))).one_or_none()


def user_from_apple(
    db: Session, identity: AppleIdentity, display_name: str | None
) -> tuple[User, bool]:
    """Find the user for an Apple identity, or create one. Returns (user, created)."""
    user = _active_user(db, User.apple_sub == identity.sub)
    if user is not None:
        return user, False
    if identity.email and identity.email_verified:
        # Same verified address as an existing email-only account: link Apple to it.
        user = _active_user(db, User.email == identity.email, User.apple_sub.is_(None))
        if user is not None:
            user.apple_sub = identity.sub
            user.email_verified_at = user.email_verified_at or now()
            return user, False
    email_taken = identity.email is not None and (
        db.scalar(select(User.id).where(User.email == identity.email)) is not None
    )
    use_email = identity.email if identity.email_verified and not email_taken else None
    user = User(
        id=uuid.uuid4(),
        apple_sub=identity.sub,
        email=use_email,
        email_verified_at=now() if use_email else None,
        display_name=(display_name or "").strip() or None,
    )
    db.add(user)
    db.flush()
    return user, True


def user_from_email(db: Session, email: str) -> tuple[User, bool]:
    """Find or create the user for a verified email address. Returns (user, created)."""
    user = _active_user(db, User.email == email)
    if user is not None:
        user.email_verified_at = user.email_verified_at or now()
        return user, False
    user = User(id=uuid.uuid4(), email=email, email_verified_at=now())
    db.add(user)
    db.flush()
    return user, True
