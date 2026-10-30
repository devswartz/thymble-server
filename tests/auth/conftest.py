"""Fixtures for auth, household and sync tests.

Database tests need DATABASE_URL (a Postgres server where the user may create databases);
they run against a throwaway database migrated to head, emptied after every test.
No test touches the network: Apple's keys are a locally generated RSA key, and email goes
to an in-memory outbox.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import make_settings
from thymble.auth.apple import APPLE_ISSUER, AppleVerifier, StaticKeySource, get_apple_verifier
from thymble.auth.email import get_email_sender
from thymble.config import Settings, normalize_db_url
from thymble.db.deps import get_db
from thymble.main import create_app
from thymble.models import Base
from thymble.ratelimit import MemoryRateLimiter, get_rate_limiter

ROOT = Path(__file__).resolve().parents[2]
BUNDLE_ID = "com.example.thymble"
APPLE_KID = "test-key-1"
PUBLIC_URL = "https://thymble.test"


def auth_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "thymble_public_url": PUBLIC_URL,
        "auth_jwt_secret": "test-auth-secret-0123456789abcdefghijklmnop",
        "powersync_jwt_secret": base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("="),
        "powersync_url": "https://sync.thymble.test",
        "apple_bundle_id": BUNDLE_ID,
        "smtp_host": "smtp.thymble.test",
        "smtp_from": "thymble@thymble.test",
    }
    values.update(overrides)
    return make_settings(**values)


# --- Apple -----------------------------------------------------------------------------


@dataclass
class FakeApple:
    """Signs identity tokens the way Apple does, with a key only the tests know."""

    private_key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    kid: str = APPLE_KID

    @property
    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        jwk.update(kid=self.kid, alg="RS256", use="sig")
        return {"keys": [jwk]}

    def verifier(self) -> AppleVerifier:
        return AppleVerifier([BUNDLE_ID], StaticKeySource(self.jwks))

    def token(self, sub: str = "apple-user-1", nonce: str | None = "n" * 32, **claims: Any) -> str:
        issued = int(time.time())
        payload: dict[str, Any] = {
            "iss": APPLE_ISSUER,
            "aud": BUNDLE_ID,
            "sub": sub,
            "iat": issued,
            "exp": issued + 600,
            "nonce": hashlib.sha256(nonce.encode()).hexdigest() if nonce is not None else None,
        }
        payload.update(claims)
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, self.private_key, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture(scope="session")
def fake_apple() -> FakeApple:
    return FakeApple()


# --- email -----------------------------------------------------------------------------


@dataclass
class Outbox:
    messages: list[dict[str, str]] = field(default_factory=list)

    def send(self, to: str, subject: str, body: str) -> None:
        self.messages.append({"to": to, "subject": subject, "body": body})

    def last_token(self) -> str:
        body = self.messages[-1]["body"]
        return body.split("token=", 1)[1].split()[0]


@pytest.fixture
def outbox() -> Outbox:
    return Outbox()


# --- database --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        pytest.skip("DATABASE_URL not set")
    admin_url = make_url(normalize_db_url(raw))
    name = f"thymble_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logging"] = False
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> Iterator[sessionmaker]:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    yield factory
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def db(session_factory: sessionmaker) -> Iterator[Session]:
    with session_factory() as session:
        yield session


def build_client(
    session_factory: sessionmaker,
    fake_apple: FakeApple,
    outbox: Outbox,
    settings: Settings | None = None,
) -> TestClient:
    app = create_app(settings or auth_settings())

    def _db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_apple_verifier] = fake_apple.verifier
    app.dependency_overrides[get_email_sender] = lambda: outbox
    # Rate-limit counters in memory, fresh for every client (no Redis in tests).
    limiter = MemoryRateLimiter()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    return TestClient(app)


@pytest.fixture
def api(session_factory: sessionmaker, fake_apple: FakeApple, outbox: Outbox) -> TestClient:
    return build_client(session_factory, fake_apple, outbox)


@dataclass
class SignedIn:
    user_id: str
    access_token: str
    refresh_token: str
    household_id: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


def sign_in(api: TestClient, fake_apple: FakeApple, sub: str, name: str | None = None) -> SignedIn:
    nonce = uuid.uuid4().hex
    response = api.post(
        "/v1/auth/apple",
        json={
            "identity_token": fake_apple.token(sub=sub, nonce=nonce),
            "nonce": nonce,
            "display_name": name,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = api.get("/v1/me", headers=headers).json()
    return SignedIn(
        user_id=body["user_id"],
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        household_id=me["households"][0]["id"],
    )


@pytest.fixture
def alice(api: TestClient, fake_apple: FakeApple) -> SignedIn:
    return sign_in(api, fake_apple, "apple-alice", "Alice")


@pytest.fixture
def bob(api: TestClient, fake_apple: FakeApple) -> SignedIn:
    return sign_in(api, fake_apple, "apple-bob", "Bob")
