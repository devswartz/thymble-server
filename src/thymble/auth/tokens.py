"""Access tokens, refresh tokens and PowerSync sync tokens."""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from thymble.config import Settings
from thymble.models import RefreshToken

ACCESS_AUDIENCE = "thymble-api"
POWERSYNC_AUDIENCE = "thymble-powersync"
# Must match the `kid` of the key in deploy/powersync/config.yaml.
POWERSYNC_KEY_ID = "thymble-api"
# The iOS app. OAuth2 clients added later (e.g. "alexa") get their own ids.
APP_CLIENT_ID = "thymble-app"


def now() -> datetime:
    return datetime.now(UTC)


def new_secret(nbytes: int = 32) -> str:
    """A random, URL-safe secret (256 bits by default)."""
    return secrets.token_urlsafe(nbytes)


def hash_secret(value: str) -> str:
    """SHA-256 hex digest. Secrets are high-entropy, so no salt or slow hash is needed."""
    return hashlib.sha256(value.encode()).hexdigest()


def _not_configured(what: str) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{what} is not configured")


# --- access tokens ---------------------------------------------------------------------


def _access_key(settings: Settings) -> str:
    secret = settings.auth_jwt_secret.get_secret_value()
    if len(secret) < 32:
        raise _not_configured("AUTH_JWT_SECRET (32+ characters)")
    return secret


def create_access_token(
    settings: Settings, user_id: uuid.UUID, client_id: str = APP_CLIENT_ID
) -> tuple[str, int]:
    issued = now()
    ttl = settings.access_token_ttl_seconds
    claims = {
        "iss": settings.thymble_public_url,
        "aud": ACCESS_AUDIENCE,
        "sub": str(user_id),
        "client_id": client_id,
        "iat": issued,
        "exp": issued + timedelta(seconds=ttl),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _access_key(settings), algorithm="HS256"), ttl


def decode_access_token(settings: Settings, token: str) -> dict[str, Any]:
    """Return the claims, or raise jwt.InvalidTokenError."""
    return jwt.decode(
        token,
        _access_key(settings),
        algorithms=["HS256"],
        audience=ACCESS_AUDIENCE,
        issuer=settings.thymble_public_url,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
    )


# --- refresh tokens --------------------------------------------------------------------


def issue_refresh_token(
    db: Session,
    settings: Settings,
    user_id: uuid.UUID,
    client_id: str,
    family_id: uuid.UUID | None = None,
) -> str:
    token = new_secret()
    db.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id or uuid.uuid4(),
            token_hash=hash_secret(token),
            client_id=client_id,
            expires_at=now() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    return token


@dataclass
class TokenPair:
    user_id: uuid.UUID
    access_token: str
    expires_in: int
    refresh_token: str


def issue_token_pair(
    db: Session,
    settings: Settings,
    user_id: uuid.UUID,
    client_id: str = APP_CLIENT_ID,
    family_id: uuid.UUID | None = None,
) -> TokenPair:
    """New access + refresh token. The caller commits."""
    access, ttl = create_access_token(settings, user_id, client_id)
    refresh = issue_refresh_token(db, settings, user_id, client_id, family_id)
    return TokenPair(user_id=user_id, access_token=access, expires_in=ttl, refresh_token=refresh)


def _invalid_refresh() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")


def rotate_refresh_token(db: Session, settings: Settings, token: str) -> TokenPair:
    """Exchange a refresh token for a new pair; the old token stops working.

    Presenting an already-rotated or revoked token is treated as theft: the whole token
    family is revoked. Commits.
    """
    row = db.scalars(
        select(RefreshToken).where(RefreshToken.token_hash == hash_secret(token)).with_for_update()
    ).one_or_none()
    if row is None:
        raise _invalid_refresh()
    current = now()
    if row.revoked_at is not None:
        revoke_family(db, row.family_id)
        db.commit()
        raise _invalid_refresh()
    if row.expires_at <= current:
        raise _invalid_refresh()
    row.revoked_at = current
    pair = issue_token_pair(db, settings, row.user_id, row.client_id, row.family_id)
    db.commit()
    return pair


def revoke_family(db: Session, family_id: uuid.UUID) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now())
    )


def revoke_refresh_token(db: Session, token: str) -> None:
    """Logout: revoke the token's family (this device's session). Commits."""
    family_id = db.scalar(
        select(RefreshToken.family_id).where(RefreshToken.token_hash == hash_secret(token))
    )
    if family_id is not None:
        revoke_family(db, family_id)
        db.commit()


def revoke_all_for_user(db: Session, user_id: uuid.UUID) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now())
    )


# --- PowerSync -------------------------------------------------------------------------


def powersync_key(settings: Settings) -> bytes:
    """The HS256 key bytes. POWERSYNC_JWT_SECRET is base64url (it is the JWK `k` value)."""
    secret = settings.powersync_jwt_secret.get_secret_value()
    try:
        key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (binascii.Error, ValueError):
        key = b""
    if len(key) < 32:
        raise _not_configured("POWERSYNC_JWT_SECRET (base64url of 32+ bytes)")
    return key


def create_powersync_token(settings: Settings, user_id: uuid.UUID) -> tuple[str, datetime]:
    issued = now()
    expires = issued + timedelta(seconds=settings.powersync_token_ttl_seconds)
    claims = {"sub": str(user_id), "aud": POWERSYNC_AUDIENCE, "iat": issued, "exp": expires}
    token = jwt.encode(
        claims, powersync_key(settings), algorithm="HS256", headers={"kid": POWERSYNC_KEY_ID}
    )
    return token, expires
