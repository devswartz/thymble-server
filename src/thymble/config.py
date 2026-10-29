"""Server configuration, read from environment variables (and an optional .env file).

Every setting has a safe default so the app can start for tests and local development.
Real secrets are only ever supplied through the environment; see `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CommaList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # General
    thymble_env: Literal["development", "production", "test"] = "production"
    thymble_public_url: str = "http://localhost:8000"
    log_level: str = "info"

    # Database. DATABASE_URL wins; otherwise it is built from the POSTGRES_* parts.
    database_url: SecretStr | None = None
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "thymble"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "thymble"

    # Job queue
    redis_url: str = "redis://redis:6379/0"

    # PowerSync: the api mints short-lived JWTs that PowerSync verifies with this key.
    powersync_url: str = "http://localhost:8080"
    powersync_jwt_secret: SecretStr = SecretStr("")

    # LLM providers (both optional; tasks route to whichever is configured)
    anthropic_api_key: SecretStr = SecretStr("")
    openai_compatible_base_url: str = ""
    openai_compatible_api_key: SecretStr = SecretStr("")
    local_model: str = ""

    # Email sign-in (magic links)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    # starttls (port 587), ssl (port 465) or none (local test servers such as Mailpit)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"

    # Auth (architecture §9). Access tokens are HS256 JWTs signed with AUTH_JWT_SECRET.
    auth_jwt_secret: SecretStr = SecretStr("")
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_days: int = 60
    powersync_token_ttl_seconds: int = 600
    # Sign in with Apple: the app's bundle id (the identity token's audience).
    # Comma-separated to also accept e.g. a Services ID. Empty turns Apple sign-in off.
    apple_bundle_id: str = ""
    # Where magic links point (a universal link the app opens); "?token=..." is appended.
    # Defaults to THYMBLE_PUBLIC_URL + "/auth/email".
    magic_link_url: str = ""
    magic_link_ttl_minutes: int = 15
    household_invite_ttl_hours: int = 168

    # Feature switches reported through /v1/capabilities
    photo_import_enabled: bool = True
    connectors_lists: CommaList = Field(default_factory=list)
    connectors_retailers: CommaList = Field(default_factory=list)
    shared_learning: bool = False

    @field_validator("connectors_lists", "connectors_retailers", mode="before")
    @classmethod
    def _split_commas(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def sqlalchemy_url(self) -> str:
        """Connection URL for SQLAlchemy (psycopg 3 driver)."""
        if self.database_url is not None:
            return normalize_db_url(self.database_url.get_secret_value())
        password = self.postgres_password.get_secret_value()
        auth = f"{self.postgres_user}:{password}" if password else self.postgres_user
        return (
            f"postgresql+psycopg://{auth}@{self.postgres_host}:{self.postgres_port}/"
            f"{self.postgres_db}"
        )

    @property
    def llm_providers(self) -> list[str]:
        providers = []
        if self.anthropic_api_key.get_secret_value():
            providers.append("anthropic")
        if self.openai_compatible_base_url:
            providers.append("openai_compatible")
        return providers

    @property
    def email_sign_in(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def apple_audiences(self) -> list[str]:
        return [part.strip() for part in self.apple_bundle_id.split(",") if part.strip()]


def normalize_db_url(url: str) -> str:
    """Accept plain postgres:// URLs and point them at the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
