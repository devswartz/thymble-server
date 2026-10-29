"""Sign-in, token refresh, logout and the current user (architecture §9, D-015)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from thymble.auth import tokens
from thymble.auth.accounts import (
    active_memberships,
    ensure_household,
    normalize_email,
    user_from_apple,
    user_from_email,
)
from thymble.auth.apple import AppleTokenError, AppleVerifier, get_apple_verifier
from thymble.auth.deps import AppSettings, CurrentUser, DbSession
from thymble.auth.email import (
    EmailSender,
    get_email_sender,
    magic_link,
    magic_link_body,
    send_quietly,
)
from thymble.config import Settings
from thymble.models import EmailLoginToken, Household, HouseholdRole, User

router = APIRouter(tags=["auth"])

# Magic-link request limits per rolling hour. The per-address limit applies whether or
# not an account exists, so hitting it reveals nothing about accounts.
EMAIL_REQUESTS_PER_ADDRESS = 5
EMAIL_REQUESTS_PER_CLIENT = 20


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str
    user_id: uuid.UUID
    new_user: bool = False


def _sign_in(db: Session, settings: Settings, user: User, created: bool) -> TokenResponse:
    ensure_household(db, user)
    pair = tokens.issue_token_pair(db, settings, user.id)
    db.commit()
    return TokenResponse(
        access_token=pair.access_token,
        expires_in=pair.expires_in,
        refresh_token=pair.refresh_token,
        user_id=user.id,
        new_user=created,
    )


# --- Sign in with Apple ----------------------------------------------------------------


class AppleSignIn(BaseModel):
    identity_token: str = Field(min_length=1, max_length=8192)
    # The raw nonce; Apple's token carries its SHA-256.
    nonce: str = Field(min_length=16, max_length=256)
    # Apple only shares the name with the app, on the first authorization.
    display_name: str | None = Field(default=None, max_length=200)


@router.post("/auth/apple")
def sign_in_with_apple(
    body: AppleSignIn,
    db: DbSession,
    settings: AppSettings,
    verifier: Annotated[AppleVerifier | None, Depends(get_apple_verifier)],
) -> TokenResponse:
    if verifier is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Sign in with Apple is off")
    try:
        identity = verifier.verify(body.identity_token, body.nonce)
    except AppleTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid Apple identity token") from exc
    user, created = user_from_apple(db, identity, body.display_name)
    return _sign_in(db, settings, user, created)


# --- Email magic link ------------------------------------------------------------------


class EmailSignInRequest(BaseModel):
    email: str = Field(max_length=320)


class EmailSignInAccepted(BaseModel):
    detail: str = "If that address can receive email, a sign-in link is on its way."


class EmailSignInVerify(BaseModel):
    token: str = Field(min_length=20, max_length=256)


def _client_fingerprint(request: Request) -> str | None:
    host = request.client.host if request.client else None
    return tokens.hash_secret(host) if host else None


@router.post("/auth/email/request", status_code=status.HTTP_202_ACCEPTED)
def request_email_sign_in(
    body: EmailSignInRequest,
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    settings: AppSettings,
    sender: Annotated[EmailSender | None, Depends(get_email_sender)],
) -> EmailSignInAccepted:
    """Email a sign-in link. Same answer whether or not an account exists."""
    if sender is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "email sign-in is off")
    try:
        email = normalize_email(body.email)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    current = tokens.now()
    window_start = current - timedelta(hours=1)
    fingerprint = _client_fingerprint(request)
    recent = select(func.count()).where(EmailLoginToken.created_at > window_start)
    per_address = db.scalar(recent.where(EmailLoginToken.email == email)) or 0
    per_client = (
        db.scalar(recent.where(EmailLoginToken.requester_hash == fingerprint)) or 0
        if fingerprint
        else 0
    )
    if per_address >= EMAIL_REQUESTS_PER_ADDRESS or per_client >= EMAIL_REQUESTS_PER_CLIENT:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many sign-in requests; try again later",
            headers={"Retry-After": "3600"},
        )

    token = tokens.new_secret()
    db.add(
        EmailLoginToken(
            email=email,
            token_hash=tokens.hash_secret(token),
            requester_hash=fingerprint,
            created_at=current,
            expires_at=current + timedelta(minutes=settings.magic_link_ttl_minutes),
        )
    )
    db.commit()
    # Sent after the response, so timing doesn't depend on the mail server either.
    background.add_task(
        send_quietly,
        sender,
        email,
        "Your Thymble sign-in link",
        magic_link_body(magic_link(settings, token), settings.magic_link_ttl_minutes),
    )
    return EmailSignInAccepted()


@router.post("/auth/email/verify")
def verify_email_sign_in(
    body: EmailSignInVerify, db: DbSession, settings: AppSettings
) -> TokenResponse:
    """Exchange a magic-link token for tokens. Each link works once, within its TTL.

    This is a POST the app makes after opening the link, never a GET on the link itself:
    mail scanners prefetch links and would otherwise burn single-use tokens.
    """
    current = tokens.now()
    email = db.scalar(
        update(EmailLoginToken)
        .where(
            EmailLoginToken.token_hash == tokens.hash_secret(body.token),
            EmailLoginToken.used_at.is_(None),
            EmailLoginToken.expires_at > current,
        )
        .values(used_at=current)
        .returning(EmailLoginToken.email)
    )
    if email is None:
        db.rollback()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired sign-in link")
    user, created = user_from_email(db, email)
    return _sign_in(db, settings, user, created)


# --- Tokens ----------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=256)


@router.post("/auth/refresh")
def refresh(body: RefreshRequest, db: DbSession, settings: AppSettings) -> TokenResponse:
    """Rotate: the presented refresh token is spent and a new pair is returned."""
    pair = tokens.rotate_refresh_token(db, settings, body.refresh_token)
    return TokenResponse(
        access_token=pair.access_token,
        expires_in=pair.expires_in,
        refresh_token=pair.refresh_token,
        user_id=pair.user_id,
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshRequest, db: DbSession) -> Response:
    """Revoke this device's refresh token (and its rotations). Access tokens expire on
    their own within ACCESS_TOKEN_TTL_SECONDS."""
    tokens.revoke_refresh_token(db, body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/auth/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_everywhere(user: CurrentUser, db: DbSession) -> Response:
    """Revoke every refresh token the user holds (all devices)."""
    tokens.revoke_all_for_user(db, user.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Current user ----------------------------------------------------------------------


class MyHousehold(BaseModel):
    id: uuid.UUID
    name: str
    role: HouseholdRole


class Me(BaseModel):
    id: uuid.UUID
    display_name: str | None
    email: str | None
    email_verified: bool
    apple_linked: bool
    households: list[MyHousehold]


class MeUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)


def _me(db: Session, user: User) -> Me:
    households = []
    for membership in active_memberships(db, user.id):
        household = db.get(Household, membership.household_id)
        assert household is not None
        households.append(MyHousehold(id=household.id, name=household.name, role=membership.role))
    return Me(
        id=user.id,
        display_name=user.display_name,
        email=user.email,
        email_verified=user.email_verified_at is not None,
        apple_linked=user.apple_sub is not None,
        households=households,
    )


@router.get("/me")
def get_me(user: CurrentUser, db: DbSession) -> Me:
    return _me(db, user)


@router.patch("/me")
def update_me(body: MeUpdate, user: CurrentUser, db: DbSession) -> Me:
    if "display_name" in body.model_fields_set:
        user.display_name = (body.display_name or "").strip() or None
        db.commit()
    return _me(db, user)
