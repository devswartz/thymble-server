"""Sign in with Apple: token verification (no network) and the sign-in endpoint."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from tests.auth.conftest import BUNDLE_ID, FakeApple, auth_settings, sign_in
from thymble.auth.apple import AppleTokenError, AppleVerifier, StaticKeySource, nonce_hash
from thymble.main import create_app

NONCE = "n" * 32


def test_valid_token_verifies(fake_apple: FakeApple):
    identity = fake_apple.verifier().verify(
        fake_apple.token(sub="abc", email="A@Example.com", email_verified="true"), NONCE
    )
    assert identity.sub == "abc"
    assert identity.email == "a@example.com"
    assert identity.email_verified is True


@pytest.mark.parametrize(
    ("claims", "nonce"),
    [
        ({"aud": "com.someone.else"}, NONCE),
        ({"iss": "https://evil.example.com"}, NONCE),
        ({"exp": int(time.time()) - 3600}, NONCE),
        ({}, "a-different-nonce-value"),
        ({"nonce": None}, NONCE),
    ],
    ids=["audience", "issuer", "expired", "wrong-nonce", "missing-nonce"],
)
def test_bad_tokens_rejected(fake_apple: FakeApple, claims: dict, nonce: str):
    with pytest.raises(AppleTokenError):
        fake_apple.verifier().verify(fake_apple.token(**claims), nonce)


def test_token_signed_by_another_key_rejected(fake_apple: FakeApple):
    impostor = FakeApple()  # same kid, different key
    with pytest.raises(AppleTokenError):
        fake_apple.verifier().verify(impostor.token(), NONCE)


def test_unknown_kid_rejected(fake_apple: FakeApple):
    other = FakeApple(kid="unknown")
    with pytest.raises(AppleTokenError):
        fake_apple.verifier().verify(other.token(), NONCE)


def test_hs256_downgrade_rejected(fake_apple: FakeApple):
    forged = jwt.encode(
        {
            "iss": "https://appleid.apple.com",
            "aud": BUNDLE_ID,
            "sub": "x",
            "nonce": nonce_hash(NONCE),
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        "secret-secret-secret-secret-secret!",
        algorithm="HS256",
        headers={"kid": fake_apple.kid},
    )
    verifier = AppleVerifier([BUNDLE_ID], StaticKeySource(fake_apple.jwks))
    with pytest.raises(AppleTokenError):
        verifier.verify(forged, NONCE)


def test_apple_off_without_bundle_id():
    client = TestClient(create_app(auth_settings(apple_bundle_id="")))
    response = client.post("/v1/auth/apple", json={"identity_token": "x", "nonce": NONCE})
    assert response.status_code == 503


@pytest.mark.db
def test_first_sign_in_creates_user_and_household(api: TestClient, fake_apple: FakeApple):
    response = api.post(
        "/v1/auth/apple",
        json={
            "identity_token": fake_apple.token(sub="new-user"),
            "nonce": NONCE,
            "display_name": "Devon",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["new_user"] is True
    assert body["token_type"] == "bearer"
    me = api.get("/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"}).json()
    assert me["display_name"] == "Devon"
    assert me["apple_linked"] is True
    assert [(h["name"], h["role"]) for h in me["households"]] == [("Devon's household", "owner")]

    again = api.post(
        "/v1/auth/apple",
        json={"identity_token": fake_apple.token(sub="new-user"), "nonce": NONCE},
    ).json()
    assert again["new_user"] is False
    assert again["user_id"] == body["user_id"]
    me = api.get("/v1/me", headers={"Authorization": f"Bearer {again['access_token']}"}).json()
    assert len(me["households"]) == 1


@pytest.mark.db
def test_invalid_token_gets_401(api: TestClient, fake_apple: FakeApple):
    response = api.post(
        "/v1/auth/apple",
        json={"identity_token": fake_apple.token(), "nonce": "not-the-right-nonce"},
    )
    assert response.status_code == 401


@pytest.mark.db
def test_apple_links_to_existing_verified_email_account(api, fake_apple, outbox):
    api.post("/v1/auth/email/request", json={"email": "devon@example.com"})
    by_email = api.post("/v1/auth/email/verify", json={"token": outbox.last_token()}).json()
    via_apple = api.post(
        "/v1/auth/apple",
        json={
            "identity_token": fake_apple.token(
                sub="apple-devon", email="devon@example.com", email_verified=True
            ),
            "nonce": NONCE,
        },
    ).json()
    assert via_apple["user_id"] == by_email["user_id"]
    assert via_apple["new_user"] is False


@pytest.mark.db
def test_sign_in_helper(api, fake_apple):
    user = sign_in(api, fake_apple, "helper-user")
    assert user.household_id
