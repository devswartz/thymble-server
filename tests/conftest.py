from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from thymble.config import Settings
from thymble.main import create_app


def make_settings(**overrides: object) -> Settings:
    """Settings isolated from the developer's environment and .env file."""
    return Settings(_env_file=None, **overrides)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(make_settings()))
