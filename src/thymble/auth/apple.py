"""Sign in with Apple: verify the identity token the iOS app gets from Apple.

Checks: RS256 signature against Apple's published keys (JWKS), issuer, audience (the
app's bundle id), expiry, and the nonce. The app generates a random nonce, sends
SHA-256(nonce) to Apple in the authorization request, and sends the raw nonce to us; the
token's `nonce` claim must equal SHA-256(raw nonce). That binds the token to this sign-in
attempt so an intercepted token can't be replayed by someone without the nonce.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Protocol

import jwt
from fastapi import Depends

from thymble.config import Settings, get_settings

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"


class AppleTokenError(Exception):
    pass


class SigningKeySource(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK: ...


class StaticKeySource:
    """Keys from a JWKS document held in memory (tests, or an air-gapped override)."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        self._keys = jwt.PyJWKSet.from_dict(jwks)

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        kid = jwt.get_unverified_header(token).get("kid")
        for key in self._keys.keys:
            if key.key_id == kid:
                return key
        raise jwt.PyJWKClientError(f"no key with kid {kid!r}")


@dataclass(frozen=True)
class AppleIdentity:
    sub: str
    email: str | None
    email_verified: bool


def nonce_hash(raw_nonce: str) -> str:
    return hashlib.sha256(raw_nonce.encode()).hexdigest()


class AppleVerifier:
    def __init__(self, audiences: list[str], keys: SigningKeySource) -> None:
        self.audiences = audiences
        self.keys = keys

    def verify(self, identity_token: str, raw_nonce: str) -> AppleIdentity:
        try:
            signing_key = self.keys.get_signing_key_from_jwt(identity_token)
            claims = jwt.decode(
                identity_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audiences,
                issuer=APPLE_ISSUER,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
                leeway=30,
            )
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
            raise AppleTokenError(str(exc)) from exc
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce, nonce_hash(raw_nonce)
        ):
            raise AppleTokenError("nonce mismatch")
        email = claims.get("email")
        # Apple sends email_verified as a boolean or as the string "true".
        verified = claims.get("email_verified") in (True, "true")
        return AppleIdentity(
            sub=str(claims["sub"]),
            email=email.lower() if isinstance(email, str) else None,
            email_verified=verified,
        )


@lru_cache
def _apple_jwks_client() -> jwt.PyJWKClient:
    # Caches the key set for an hour and refetches once when it sees an unknown kid.
    return jwt.PyJWKClient(APPLE_JWKS_URL, cache_jwk_set=True, lifespan=3600, timeout=10)


def get_apple_verifier(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AppleVerifier | None:
    """None when APPLE_BUNDLE_ID is unset (Apple sign-in off). Tests override this."""
    if not settings.apple_audiences:
        return None
    return AppleVerifier(settings.apple_audiences, _apple_jwks_client())
