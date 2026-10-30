"""Access tokens, refresh rotation, logout, /v1/me and the PowerSync token."""

from __future__ import annotations

import base64
import uuid
from datetime import timedelta

import jwt
import pytest
from sqlalchemy import update

from tests.auth.conftest import auth_settings, sign_in
from thymble.auth.tokens import create_access_token, create_powersync_token, now
from thymble.models import RefreshToken


def test_powersync_token_matches_powersync_config():
    settings = auth_settings()
    token, _ = create_powersync_token(settings, uuid.uuid4())
    secret = settings.powersync_jwt_secret.get_secret_value()
    key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    header = jwt.get_unverified_header(token)
    assert header["kid"] == "thymble-api" and header["alg"] == "HS256"
    claims = jwt.decode(token, key, algorithms=["HS256"], audience="thymble-powersync")
    assert claims["exp"] - claims["iat"] == settings.powersync_token_ttl_seconds


@pytest.mark.db
def test_me_requires_token(api):
    assert api.get("/v1/me").status_code == 401
    assert api.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401


@pytest.mark.db
def test_expired_access_token_rejected(api, alice):
    settings = auth_settings(access_token_ttl_seconds=-120)
    token, _ = create_access_token(settings, uuid.UUID(alice.user_id))
    assert api.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.db
def test_token_from_other_server_rejected(api, alice):
    other = auth_settings(auth_jwt_secret="another-servers-secret-0123456789abcdef")
    token, _ = create_access_token(other, uuid.UUID(alice.user_id))
    assert api.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.db
def test_me_and_rename(api, alice):
    me = api.get("/v1/me", headers=alice.headers).json()
    assert me["id"] == alice.user_id
    assert me["display_name"] == "Alice"
    renamed = api.patch("/v1/me", json={"display_name": "Al"}, headers=alice.headers).json()
    assert renamed["display_name"] == "Al"


@pytest.mark.db
def test_refresh_rotates(api, alice):
    first = api.post("/v1/auth/refresh", json={"refresh_token": alice.refresh_token})
    assert first.status_code == 200
    body = first.json()
    assert body["user_id"] == alice.user_id
    assert body["refresh_token"] != alice.refresh_token
    assert api.get("/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"}).is_success
    # The new refresh token works once too.
    second = api.post("/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert second.status_code == 200


@pytest.mark.db
def test_refresh_reuse_revokes_family(api, alice, db):
    rotated = api.post("/v1/auth/refresh", json={"refresh_token": alice.refresh_token}).json()
    # Outside the reuse grace window (test_refresh_grace.py covers the window itself).
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.revoked_at.is_not(None))
        .values(revoked_at=now() - timedelta(minutes=5))
    )
    db.commit()
    # Someone replays the old token: rejected, and the rotated one dies with it.
    replay = api.post("/v1/auth/refresh", json={"refresh_token": alice.refresh_token})
    assert replay.status_code == 401
    after = api.post("/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert after.status_code == 401


@pytest.mark.db
def test_expired_refresh_rejected(api, alice, db):
    db.execute(update(RefreshToken).values(expires_at=now() - timedelta(seconds=1)))
    db.commit()
    response = api.post("/v1/auth/refresh", json={"refresh_token": alice.refresh_token})
    assert response.status_code == 401


@pytest.mark.db
def test_logout_revokes_refresh(api, alice):
    assert (
        api.post("/v1/auth/logout", json={"refresh_token": alice.refresh_token}).status_code == 204
    )
    response = api.post("/v1/auth/refresh", json={"refresh_token": alice.refresh_token})
    assert response.status_code == 401
    # Logging out twice (or with an unknown token) is harmless.
    assert api.post("/v1/auth/logout", json={"refresh_token": "z" * 43}).status_code == 204


@pytest.mark.db
def test_logout_all(api, fake_apple, alice):
    second_device = sign_in(api, fake_apple, "apple-alice")
    assert api.post("/v1/auth/logout-all", headers=alice.headers).status_code == 204
    for token in (alice.refresh_token, second_device.refresh_token):
        assert api.post("/v1/auth/refresh", json={"refresh_token": token}).status_code == 401
