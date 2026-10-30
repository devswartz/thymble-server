"""Households: list, create, rename, members and roles, invites, join and leave.

Owners manage the household (rename, invite, change roles, remove members); every member
can see members and leave. Non-members get 404 for everything about a household.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from thymble.auth.accounts import active_memberships, create_household
from thymble.auth.deps import AppSettings, CurrentUser, DbSession, require_member
from thymble.auth.tokens import hash_secret, now
from thymble.models import Household, HouseholdInvite, HouseholdMember, HouseholdRole, User
from thymble.ratelimit import Hit, Limiter, client_ip, enforce, parse_limit

router = APIRouter(prefix="/households", tags=["households"])

# Invite codes: 12 characters from an alphabet without look-alikes (~60 bits), shown as
# XXXX-XXXX-XXXX. Only the hash is stored.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_CODE_LENGTH = 12


class HouseholdOut(BaseModel):
    id: uuid.UUID
    name: str
    role: HouseholdRole


class HouseholdCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class MemberOut(BaseModel):
    user_id: uuid.UUID
    display_name: str | None
    role: HouseholdRole
    joined_at: datetime


class RoleUpdate(BaseModel):
    role: HouseholdRole


class InviteOut(BaseModel):
    id: uuid.UUID
    code: str
    url: str
    expires_at: datetime


class JoinRequest(BaseModel):
    code: str = Field(min_length=_CODE_LENGTH, max_length=32)


def _out(household: Household, role: HouseholdRole) -> HouseholdOut:
    return HouseholdOut(id=household.id, name=household.name, role=role)


def _household(db: Session, household_id: uuid.UUID) -> Household:
    household = db.get(Household, household_id)
    assert household is not None
    return household


def _owner_count(db: Session, household_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count()).where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.role == HouseholdRole.OWNER,
                HouseholdMember.deleted_at.is_(None),
            )
        )
        or 0
    )


def _member_count(db: Session, household_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count()).where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.deleted_at.is_(None),
            )
        )
        or 0
    )


def _active_member(db: Session, household_id: uuid.UUID, user_id: uuid.UUID) -> HouseholdMember:
    member = db.scalars(
        select(HouseholdMember).where(
            HouseholdMember.household_id == household_id,
            HouseholdMember.user_id == user_id,
            HouseholdMember.deleted_at.is_(None),
        )
    ).one_or_none()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    return member


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


@router.get("")
def list_households(user: CurrentUser, db: DbSession) -> list[HouseholdOut]:
    return [_out(_household(db, m.household_id), m.role) for m in active_memberships(db, user.id)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: HouseholdCreate, user: CurrentUser, db: DbSession) -> HouseholdOut:
    household = create_household(db, user, body.name.strip())
    db.commit()
    return _out(household, HouseholdRole.OWNER)


@router.patch("/{household_id}")
def rename(
    household_id: uuid.UUID, body: HouseholdCreate, user: CurrentUser, db: DbSession
) -> HouseholdOut:
    membership = require_member(db, user, household_id, HouseholdRole.OWNER)
    household = _household(db, household_id)
    household.name = body.name.strip()
    db.commit()
    return _out(household, membership.role)


@router.get("/{household_id}/members")
def list_members(household_id: uuid.UUID, user: CurrentUser, db: DbSession) -> list[MemberOut]:
    require_member(db, user, household_id)
    rows = db.execute(
        select(HouseholdMember, User)
        .join(User, User.id == HouseholdMember.user_id)
        .where(HouseholdMember.household_id == household_id, HouseholdMember.deleted_at.is_(None))
        .order_by(HouseholdMember.created_at)
    )
    return [
        MemberOut(
            user_id=member.user_id,
            display_name=member_user.display_name,
            role=member.role,
            joined_at=member.created_at,
        )
        for member, member_user in rows
    ]


@router.patch("/{household_id}/members/{user_id}")
def change_role(
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    body: RoleUpdate,
    user: CurrentUser,
    db: DbSession,
) -> MemberOut:
    require_member(db, user, household_id, HouseholdRole.OWNER)
    member = _active_member(db, household_id, user_id)
    if (
        member.role is HouseholdRole.OWNER
        and body.role is not HouseholdRole.OWNER
        and _owner_count(db, household_id) <= 1
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "a household needs at least one owner")
    member.role = body.role
    db.commit()
    member_user = db.get(User, user_id)
    return MemberOut(
        user_id=user_id,
        display_name=member_user.display_name if member_user else None,
        role=member.role,
        joined_at=member.created_at,
    )


@router.delete("/{household_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    household_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    """Owners remove someone else. To remove yourself, use /leave."""
    require_member(db, user, household_id, HouseholdRole.OWNER)
    if user_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "use /leave to leave a household")
    member = _active_member(db, household_id, user_id)
    member.deleted_at = now()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{household_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave(household_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    """Leave a household. The last owner must hand over ownership first, unless they are
    the last member, in which case the household is deleted."""
    membership = require_member(db, user, household_id)
    remaining = _member_count(db, household_id) - 1
    if (
        remaining > 0
        and membership.role is HouseholdRole.OWNER
        and _owner_count(db, household_id) <= 1
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "make another member an owner before leaving")
    current = now()
    membership.deleted_at = current
    if remaining == 0:
        _household(db, household_id).deleted_at = current
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{household_id}/invites", status_code=status.HTTP_201_CREATED)
def create_invite(
    household_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: AppSettings
) -> InviteOut:
    """A single-use invite code (and link) that lets one person join."""
    require_member(db, user, household_id, HouseholdRole.OWNER)
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    code = "-".join(raw[i : i + 4] for i in range(0, _CODE_LENGTH, 4))
    invite = HouseholdInvite(
        id=uuid.uuid4(),
        household_id=household_id,
        code_hash=hash_secret(raw),
        created_by=user.id,
        expires_at=now() + timedelta(hours=settings.household_invite_ttl_hours),
    )
    db.add(invite)
    db.commit()
    url = f"{settings.thymble_public_url.rstrip('/')}/join?code={code}"
    return InviteOut(id=invite.id, code=code, url=url, expires_at=invite.expires_at)


@router.delete("/{household_id}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite(
    household_id: uuid.UUID, invite_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    require_member(db, user, household_id, HouseholdRole.OWNER)
    invite = db.get(HouseholdInvite, invite_id)
    if invite is None or invite.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    if invite.revoked_at is None and invite.used_at is None:
        invite.revoked_at = now()
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/join")
def join(
    body: JoinRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    limiter: Limiter,
) -> HouseholdOut:
    """Redeem an invite code. Each code works once.

    Rate-limited per user (RATE_LIMIT_INVITE_JOIN_PER_USER) and per client address
    (RATE_LIMIT_INVITE_JOIN_PER_IP) so codes can't be guessed by brute force.
    """
    enforce(
        limiter,
        [
            Hit(
                "invite_join_user",
                parse_limit(settings.rate_limit_invite_join_per_user),
                str(user.id),
            ),
            Hit(
                "invite_join_ip",
                parse_limit(settings.rate_limit_invite_join_per_ip),
                client_ip(request, settings),
            ),
        ],
        "too many join attempts; try again later",
    )
    current = now()
    invite = db.scalars(
        select(HouseholdInvite)
        .where(HouseholdInvite.code_hash == hash_secret(normalize_code(body.code)))
        .with_for_update()
    ).one_or_none()
    household = db.get(Household, invite.household_id) if invite else None
    if (
        invite is None
        or household is None
        or household.deleted_at is not None
        or invite.used_at is not None
        or invite.revoked_at is not None
        or invite.expires_at <= current
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite code is invalid or has expired")
    existing = db.scalars(
        select(HouseholdMember).where(
            HouseholdMember.household_id == household.id, HouseholdMember.user_id == user.id
        )
    ).one_or_none()
    if existing is not None and existing.deleted_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "already a member of this household")
    invite.used_at = current
    invite.used_by = user.id
    if existing is not None:
        # Rejoining: revive the old row (unique per household + user).
        existing.deleted_at = None
        existing.role = HouseholdRole.MEMBER
    else:
        db.add(
            HouseholdMember(household_id=household.id, user_id=user.id, role=HouseholdRole.MEMBER)
        )
    db.commit()
    return _out(household, HouseholdRole.MEMBER)
