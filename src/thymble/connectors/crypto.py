"""Encryption at rest for connector credentials (architecture §12).

The key comes from CONNECTOR_ENCRYPTION_KEY. Any random string works: a Fernet key is
derived from it with SHA-256, so `scripts/dev_env.py` placeholders and real Fernet keys
are both accepted. Changing the variable makes existing credentials unreadable (accounts
then need their token re-entered).
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from thymble.config import Settings


class CredentialsUnavailable(Exception):
    """No key configured, or the stored credentials can't be decrypted with it."""


def _fernet(settings: Settings) -> Fernet:
    secret = settings.connector_encryption_key.get_secret_value()
    if not secret:
        raise CredentialsUnavailable("CONNECTOR_ENCRYPTION_KEY is not set")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_credentials(settings: Settings, credentials: dict[str, Any]) -> bytes:
    return _fernet(settings).encrypt(json.dumps(credentials).encode())


def decrypt_credentials(settings: Settings, blob: bytes | None) -> dict[str, Any]:
    if not blob:
        raise CredentialsUnavailable("no credentials stored")
    try:
        return json.loads(_fernet(settings).decrypt(bytes(blob)))
    except InvalidToken as exc:
        raise CredentialsUnavailable("credentials can't be decrypted with this key") from exc
