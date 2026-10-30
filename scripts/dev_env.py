"""Write a .env for local development from .env.example, with random dev-only secrets.

Usage (from the repo root):  python scripts/dev_env.py [--force]

Every `change-me` placeholder gets a fresh random value, and THYMBLE_ENV is set to
`development`. The result is for your machine only: .env is git-ignored, never commit it.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"

OVERRIDES = {
    "THYMBLE_ENV": "development",
    "LOG_LEVEL": "debug",
    "THYMBLE_PUBLIC_URL": "http://localhost:8000",
    "POWERSYNC_URL": "http://localhost:8080",
    # Magic-link email goes to the Mailpit container from docker-compose.override.yml
    # (web UI on http://localhost:8025).
    "SMTP_HOST": "mailpit",
    "SMTP_PORT": "1025",
    "SMTP_SECURITY": "none",
}


def main() -> int:
    if TARGET.exists() and "--force" not in sys.argv:
        print(f"{TARGET} already exists; pass --force to overwrite it.")
        return 1
    lines = []
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            if key in OVERRIDES:
                value = OVERRIDES[key]
            elif value == "change-me":
                # base64url without padding: valid as a password and as a JWK "k" value
                value = secrets.token_urlsafe(32)
            line = f"{key}={value}"
        lines.append(line)
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {TARGET} with random development secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
