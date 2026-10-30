"""The committed OpenAPI document (docs/openapi.json) matches the code."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _exporter():
    spec = importlib.util.spec_from_file_location(
        "export_openapi", ROOT / "scripts" / "export_openapi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_openapi_is_current():
    exporter = _exporter()
    committed = exporter.OUTPUT.read_text(encoding="utf-8")
    assert committed == exporter.render(), (
        "docs/openapi.json is stale: run `uv run python scripts/export_openapi.py` and commit it"
    )


def test_openapi_document_shape():
    document = json.loads(_exporter().render())
    assert document["info"]["title"] == "Thymble Server"
    assert document["info"]["license"]["identifier"] == "AGPL-3.0-only"
    paths = document["paths"]
    for path in ("/healthz", "/v1/capabilities", "/v1/auth/refresh", "/v1/households/join"):
        assert path in paths
    # Rate-limited endpoints document nothing extra, but must still be present as POSTs.
    assert "post" in paths["/v1/auth/email/request"]


def test_render_is_deterministic():
    exporter = _exporter()
    assert exporter.render() == exporter.render()
