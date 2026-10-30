"""Refresh-token reuse grace: racing refreshes share one new pair; real reuse is theft."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from tests.auth.conftest import auth_settings, build_client, sign_in
from thymble.auth import tokens
from thymble.auth.tokens import hash_secret, now
from thymble.models import RefreshToken

pytestmark = pytest.mark.db


def refresh(api, token: str):
    return api.post("/v1/auth/refresh", json={"refresh_token": token})


def age_rotation(db, token: str, seconds: float) -> None:
    """Pretend `token` was rotated `seconds` ago."""
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_secret(token))
        .values(revoked_at=now() - timedelta(seconds=seconds))
    )
    db.commit()


def test_reuse_within_grace_gets_the_same_pair(api, alice):
    first = refresh(api, alice.refresh_token)
    second = refresh(api, alice.refresh_token)
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["refresh_token"] == b["refresh_token"] != alice.refresh_token
    assert a["access_token"] == b["access_token"]
    assert a["user_id"] == b["user_id"] == alice.user_id
    assert 0 < b["expires_in"] <= a["expires_in"]
    # Both copies work, and the family is intact: the shared successor rotates normally.
    me = api.get("/v1/me", headers={"Authorization": f"Bearer {b['access_token']}"})
    assert me.status_code == 200
    assert refresh(api, b["refresh_token"]).status_code == 200


def test_grace_reuse_does_not_mint_extra_tokens(api, alice, db):
    refresh(api, alice.refresh_token)
    refresh(api, alice.refresh_token)
    refresh(api, alice.refresh_token)
    family = db.scalar(
        select(RefreshToken.family_id).where(
            RefreshToken.token_hash == hash_secret(alice.refresh_token)
        )
    )
    rows = db.scalars(select(RefreshToken).where(RefreshToken.family_id == family)).all()
    assert len(rows) == 2
    assert sum(row.revoked_at is None for row in rows) == 1


def test_reuse_after_grace_is_theft(api, alice, db):
    rotated = refresh(api, alice.refresh_token).json()
    age_rotation(db, alice.refresh_token, 31)
    assert refresh(api, alice.refresh_token).status_code == 401
    # The whole family is revoked, including the legitimate successor.
    assert refresh(api, rotated["refresh_token"]).status_code == 401


def test_reuse_just_inside_grace_succeeds(api, alice, db):
    rotated = refresh(api, alice.refresh_token).json()
    age_rotation(db, alice.refresh_token, 28)
    again = refresh(api, alice.refresh_token)
    assert again.status_code == 200
    assert again.json()["refresh_token"] == rotated["refresh_token"]
    assert again.json()["expires_in"] <= 900 - 28


def test_grace_window_is_configurable(session_factory, fake_apple, outbox, db):
    api = build_client(
        session_factory, fake_apple, outbox, auth_settings(refresh_reuse_grace_seconds=120)
    )
    alice = sign_in(api, fake_apple, "apple-alice")
    refresh(api, alice.refresh_token)
    age_rotation(db, alice.refresh_token, 90)
    assert refresh(api, alice.refresh_token).status_code == 200
    age_rotation(db, alice.refresh_token, 121)
    assert refresh(api, alice.refresh_token).status_code == 401


def test_grace_off_restores_strict_reuse_detection(session_factory, fake_apple, outbox):
    api = build_client(
        session_factory, fake_apple, outbox, auth_settings(refresh_reuse_grace_seconds=0)
    )
    alice = sign_in(api, fake_apple, "apple-alice")
    rotated = refresh(api, alice.refresh_token).json()
    assert refresh(api, alice.refresh_token).status_code == 401
    assert refresh(api, rotated["refresh_token"]).status_code == 401


def test_no_grace_once_successor_has_rotated(api, alice):
    """A replay of an older generation is theft even inside the window."""
    second = refresh(api, alice.refresh_token).json()
    third = refresh(api, second["refresh_token"]).json()
    assert refresh(api, alice.refresh_token).status_code == 401
    assert refresh(api, third["refresh_token"]).status_code == 401


def test_no_grace_after_logout(api, alice):
    rotated = refresh(api, alice.refresh_token).json()
    assert (
        api.post("/v1/auth/logout", json={"refresh_token": rotated["refresh_token"]}).status_code
        == 204
    )
    assert refresh(api, alice.refresh_token).status_code == 401
    assert refresh(api, rotated["refresh_token"]).status_code == 401


def test_no_grace_for_a_token_revoked_without_rotation(api, alice):
    """Logout revokes the token itself (no successor): reuse is refused at once."""
    api.post("/v1/auth/logout", json={"refresh_token": alice.refresh_token})
    assert refresh(api, alice.refresh_token).status_code == 401


def test_no_grace_after_logout_all(api, alice):
    refresh(api, alice.refresh_token)
    api.post("/v1/auth/logout-all", headers=alice.headers)
    assert refresh(api, alice.refresh_token).status_code == 401


def test_other_devices_unaffected_by_grace(api, fake_apple, alice):
    other = sign_in(api, fake_apple, "apple-alice")
    a = refresh(api, alice.refresh_token).json()
    b = refresh(api, other.refresh_token).json()
    assert a["refresh_token"] != b["refresh_token"]


def test_successor_is_derived_from_the_secret(api, alice, db):
    """The successor depends on AUTH_JWT_SECRET, so a stolen token alone can't predict it."""
    settings_a = auth_settings()
    settings_b = auth_settings(auth_jwt_secret="another-secret-0123456789abcdefghijklmn")
    one = tokens._successor(settings_a, alice.refresh_token)
    assert one == tokens._successor(settings_a, alice.refresh_token)
    assert one != tokens._successor(settings_b, alice.refresh_token)
    assert one == refresh(api, alice.refresh_token).json()["refresh_token"]


def test_concurrent_refreshes_both_succeed(session_factory, alice):
    """Two devices refreshing the same token at once get the same pair (row lock + grace)."""
    settings = auth_settings()
    barrier = threading.Barrier(2)
    results: list[object] = [None, None]

    def worker(i: int) -> None:
        with session_factory() as session:
            barrier.wait()
            try:
                results[i] = tokens.rotate_refresh_token(session, settings, alice.refresh_token)
            except Exception as exc:
                results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    first, second = results
    assert isinstance(first, tokens.TokenPair), first
    assert isinstance(second, tokens.TokenPair), second
    assert first.refresh_token == second.refresh_token
    assert first.access_token == second.access_token
