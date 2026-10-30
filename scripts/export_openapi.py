"""Write the api's OpenAPI document to docs/openapi.json.

    uv run python scripts/export_openapi.py          # rewrite the file
    uv run python scripts/export_openapi.py --check  # exit 1 if the file is stale

tests/test_openapi.py runs the check, so commit the regenerated file with any change to
routes or request/response models.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "openapi.json"


def render() -> str:
    """The OpenAPI document as stable, pretty-printed JSON (settings don't affect it)."""
    from thymble.config import Settings
    from thymble.main import create_app

    app = create_app(Settings(_env_file=None))
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args(argv)
    document = render()
    current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else None
    if args.check:
        if current != document:
            print(f"{OUTPUT.relative_to(ROOT)} is stale; run scripts/export_openapi.py")
            return 1
        return 0
    if current != document:
        OUTPUT.write_text(document, encoding="utf-8", newline="\n")
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
