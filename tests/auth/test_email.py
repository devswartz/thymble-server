"""Email magic-link sign-in."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tests.auth.conftest import PUBLIC_URL, auth_settings, build_client
from thymble.auth.email import get_email_sender, magic_link
from thymble.auth.tokens import now
from thymble.main import create_app
from thymble.models import EmailLoginToken

pytestmark = pytest.mark.db

# The defaults: RATE_LIMIT_MAGIC_LINK_PER_EMAIL=5/hour, RATE_LIMIT_MAGIC_LINK_PER_IP=20/hour
EMAIL_REQUESTS_PER_ADDRESS = 5
EMAIL_REQUESTS_PER_CLIENT = 20


def request_link(api: TestClient, email: str):
    return api.post("/v1/auth/email/request", json={"email": email})


def test_request_then_verify_signs_in(api, outbox, db):
    response = request_link(api, "  Cook@Example.COM ")
    assert response.status_code == 202
    assert len(outbox.messages) == 1
    message = outbox.messages[0]
    assert message["to"] == "cook@example.com"
    assert f"{PUBLIC_URL}/auth/email?token=" in message["body"]
    # Only the hash is stored.
    token = outbox.last_token()
    stored = db.scalars(select(EmailLoginToken.token_hash)).all()
    assert token not in stored and len(stored) == 1

    verified = api.post("/v1/auth/email/verify", json={"token": token})
    assert verified.status_code == 200
    body = verified.json()
    assert body["new_user"] is True
    me = api.get("/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"}).json()
    assert me["email"] == "cook@example.com"
    assert me["email_verified"] is True
    assert len(me["households"]) == 1


def test_link_is_single_use(api, outbox):
    request_link(api, "once@example.com")
    token = outbox.last_token()
    assert api.post("/v1/auth/email/verify", json={"token": token}).status_code == 200
    assert api.post("/v1/auth/email/verify", json={"token": token}).status_code == 401


def test_expired_link_rejected(api, outbox, db):
    request_link(api, "late@example.com")
    db.execute(update(EmailLoginToken).values(expires_at=now() - timedelta(seconds=1)))
    db.commit()
    response = api.post("/v1/auth/email/verify", json={"token": outbox.last_token()})
    assert response.status_code == 401


def test_bogus_token_rejected(api):
    response = api.post("/v1/auth/email/verify", json={"token": "x" * 43})
    assert response.status_code == 401


def test_no_account_enumeration(api, outbox):
    # Existing and unknown addresses get the same response.
    request_link(api, "known@example.com")
    api.post("/v1/auth/email/verify", json={"token": outbox.last_token()})
    known = request_link(api, "known@example.com")
    unknown = request_link(api, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


def test_rate_limited_per_address(api):
    for _ in range(EMAIL_REQUESTS_PER_ADDRESS):
        assert request_link(api, "spam@example.com").status_code == 202
    limited = request_link(api, "spam@example.com")
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    # The limit is per address: other addresses still go through.
    assert request_link(api, "other@example.com").status_code == 202


def test_rate_limited_per_client(api):
    for i in range(EMAIL_REQUESTS_PER_CLIENT):
        assert request_link(api, f"user{i}@example.com").status_code == 202
    assert request_link(api, "one-more@example.com").status_code == 429


def test_invalid_email_rejected(api):
    assert request_link(api, "not-an-email").status_code == 422


def test_email_sign_in_off_without_smtp(session_factory, fake_apple, outbox):
    settings = auth_settings(smtp_host="", smtp_from="")
    client = TestClient(create_app(settings))
    assert client.get("/v1/capabilities").json()["email_sign_in"] is False
    # Without an override the real dependency reports email sign-in as off.
    api = build_client(session_factory, fake_apple, outbox, settings)
    del api.app.dependency_overrides[get_email_sender]
    assert request_link(api, "a@example.com").status_code == 503


def test_magic_link_url_override():
    settings = auth_settings(magic_link_url="https://app.example.com/signin?src=mail")
    assert magic_link(settings, "abc") == "https://app.example.com/signin?src=mail&token=abc"
