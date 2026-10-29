"""The PowerSync download config: soft deletes propagate, server-only tables never sync."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from thymble.models import Base
from thymble.sync import registered_tables

CONFIG = Path(__file__).resolve().parent.parent / "deploy" / "powersync" / "sync-config.yaml"

# Never downloaded to devices: secrets, raw imports, metering, cross-household caches,
# connector credentials and external ids, the server's learning log.
SERVER_ONLY = {
    "app_user",
    "auth_email_token",
    "auth_refresh_token",
    "household_invite",
    "import_job",
    "llm_usage",
    "llm_cache",
    "connector_account",
    "connector_item_link",
    "learning_event",
}

_FROM = re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE)


def _stream_queries() -> list[str]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    queries = []
    for stream in config["streams"].values():
        queries.extend(stream.get("queries", []))
        if "query" in stream:
            queries.append(stream["query"])
    return queries


def _synced_tables() -> set[str]:
    return {_FROM.search(q).group(1) for q in _stream_queries()}


def test_every_query_skips_soft_deleted_rows():
    for query in _stream_queries():
        assert re.search(r"\bdeleted_at IS NULL\b", query), query


def test_server_only_tables_are_never_synced():
    config_text = CONFIG.read_text(encoding="utf-8")
    queried = set(_FROM.findall(config_text))
    assert not queried & SERVER_ONLY
    assert not {t for t in queried if t.startswith("auth_")}


def test_every_table_is_classified():
    """A new table must be added to the sync config or to SERVER_ONLY deliberately."""
    tables = set(Base.metadata.tables)
    assert tables == _synced_tables() | SERVER_ONLY, tables ^ (_synced_tables() | SERVER_ONLY)


def test_writable_tables_are_downloaded():
    assert set(registered_tables()) <= _synced_tables()
