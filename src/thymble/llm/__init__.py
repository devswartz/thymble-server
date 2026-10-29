"""LLM module: one entry point for every model call (architecture §10, D-017, D-019).

client = get_llm_client()
result = await client.run(Task.T3, system=..., prompt=..., output=MyModel, household_id=hh)
if result.ok: use(result.data) else: mark for review (result.reason)
"""

from __future__ import annotations

from functools import lru_cache

from thymble.config import get_settings
from thymble.llm.client import DbStore, LLMClient, LLMResult, MemoryStore, strict_schema
from thymble.llm.providers import ImageInput
from thymble.llm.tasks import Task


@lru_cache
def get_llm_client() -> LLMClient:
    """Process-wide client with the Postgres-backed cache and meter."""
    from thymble.db.session import get_sessionmaker

    return LLMClient(get_settings(), store=DbStore(get_sessionmaker()))


__all__ = [
    "DbStore",
    "ImageInput",
    "LLMClient",
    "LLMResult",
    "MemoryStore",
    "Task",
    "get_llm_client",
    "strict_schema",
]
