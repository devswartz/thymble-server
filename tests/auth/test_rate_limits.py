"""Rate limits on magic-link requests and household invite joins (through the API)."""

from __future__ import annotations

import pytest

from tests.auth.conftest import auth_settings, build_client, sign_in
from thymble.ratelimit import get_rate_limiter

pytestmark = pytest.mark.db

CF = "CF-Connecting-IP"


@pytest.fixture
def limited(session_factory, fake_apple, outbox):
    """A client with small limits, reading the client address from CF-Connecting-IP."""

    def make(**overrides):
        settings = auth_settings(client_ip_header=CF, **overrides)
        return build_client(session_factory, fake_apple, outbox, settings)

    return make


def request_link(api, email: str, ip: str = "203.0.113.1"):
    return api.post("/v1/auth/email/request", json={"email": email}, headers={CF: ip})


# --- magic links -----------------------------------------------------------------------


def test_magic_link_per_email_limit(limited, outbox):
    api = limited(rate_limit_magic_link_per_email="2/hour")
    # Different client addresses don't help: the address is counted.
    assert request_link(api, "cook@example.com", "203.0.113.1").status_code == 202
    assert request_link(api, "Cook@Example.com", "203.0.113.2").status_code == 202
    blocked = request_link(api, "cook@example.com", "203.0.113.3")
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["Retry-After"]) <= 3600
    assert len(outbox.messages) == 2
    assert request_link(api, "other@example.com").status_code == 202


def test_magic_link_per_ip_limit(limited):
    api = limited(rate_limit_magic_link_per_ip="3/10min")
    for i in range(3):
        assert request_link(api, f"u{i}@example.com", "198.51.100.7").status_code == 202
    blocked = request_link(api, "u9@example.com", "198.51.100.7")
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["Retry-After"]) <= 600
    # Another client is unaffected.
    assert request_link(api, "u9@example.com", "198.51.100.8").status_code == 202


def test_magic_link_limit_reveals_nothing_about_accounts(limited, outbox):
    api = limited(rate_limit_magic_link_per_email="1/hour")
    # An existing account and an unknown address hit the limit the same way.
    assert request_link(api, "known@example.com").status_code == 202
    api.post("/v1/auth/email/verify", json={"token": outbox.last_token()})
    known = request_link(api, "known@example.com")
    assert request_link(api, "nobody@example.com").status_code == 202
    unknown = request_link(api, "nobody@example.com")
    assert known.status_code == unknown.status_code == 429
    assert known.json() == unknown.json()


def test_magic_link_limits_can_be_turned_off(limited):
    api = limited(rate_limit_magic_link_per_email="0", rate_limit_magic_link_per_ip="0")
    for _ in range(8):
        assert request_link(api, "cook@example.com").status_code == 202


def test_magic_link_rejected_request_does_not_count_invalid_email(limited):
    api = limited(rate_limit_magic_link_per_ip="1/hour")
    assert request_link(api, "not-an-email").status_code == 422
    assert request_link(api, "fine@example.com").status_code == 202


def test_rate_limiting_disabled_entirely(session_factory, fake_apple, outbox):
    settings = auth_settings(rate_limit_enabled=False, rate_limit_magic_link_per_email="1/hour")
    api = build_client(session_factory, fake_apple, outbox, settings)
    # Use the real dependency (not the test's in-memory override) to check the switch.
    api.app.dependency_overrides.pop(get_rate_limiter)
    for _ in range(3):
        assert request_link(api, "cook@example.com").status_code == 202


# --- invite joins ----------------------------------------------------------------------


def join(api, user, code: str, ip: str = "203.0.113.1"):
    return api.post("/v1/households/join", json={"code": code}, headers={**user.headers, CF: ip})


def invite(api, owner) -> str:
    response = api.post(f"/v1/households/{owner.household_id}/invites", headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()["code"]


def test_join_per_user_limit_stops_guessing(limited, fake_apple):
    api = limited(rate_limit_invite_join_per_user="3/hour")
    alice = sign_in(api, fake_apple, "apple-alice", "Alice")
    mallory = sign_in(api, fake_apple, "apple-mallory", "Mallory")
    for i in range(3):
        assert join(api, mallory, f"WRNG-CODE-{i:04d}", f"203.0.113.{i}").status_code == 404
    # Even a valid code is refused once the user is over the limit.
    code = invite(api, alice)
    blocked = join(api, mallory, code, "203.0.113.99")
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["Retry-After"]) <= 3600
    # The code wasn't spent: someone else can still use it.
    bob = sign_in(api, fake_apple, "apple-bob", "Bob")
    assert join(api, bob, code, "203.0.113.50").status_code == 200


def test_join_per_ip_limit(limited, fake_apple):
    api = limited(rate_limit_invite_join_per_ip="2/hour")
    users = [sign_in(api, fake_apple, f"apple-{i}") for i in range(3)]
    assert join(api, users[0], "AAAA-BBBB-CCCC", "198.51.100.7").status_code == 404
    assert join(api, users[1], "AAAA-BBBB-CCCC", "198.51.100.7").status_code == 404
    assert join(api, users[2], "AAAA-BBBB-CCCC", "198.51.100.7").status_code == 429
    assert join(api, users[2], "AAAA-BBBB-CCCC", "198.51.100.8").status_code == 404


def test_join_requires_sign_in_before_counting(limited):
    api = limited(rate_limit_invite_join_per_ip="1/hour")
    for _ in range(3):
        response = api.post(
            "/v1/households/join", json={"code": "AAAA-BBBB-CCCC"}, headers={CF: "1.2.3.4"}
        )
        assert response.status_code == 401


def test_join_still_works_under_the_limit(limited, fake_apple):
    api = limited()
    alice = sign_in(api, fake_apple, "apple-alice", "Alice")
    bob = sign_in(api, fake_apple, "apple-bob", "Bob")
    assert join(api, bob, invite(api, alice)).status_code == 200
