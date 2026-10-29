from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from thymble.config import Settings
from thymble.main import create_app


def make_settings(**overrides: object) -> Settings:
    """Settings isolated from the developer's environment and .env file."""
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore app settings from the environment (e.g. the dev container's .env values).

    DATABASE_URL stays: database tests use it to find the server.
    """
    for name in Settings.model_fields:
        if name != "database_url":
            monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(make_settings()))
