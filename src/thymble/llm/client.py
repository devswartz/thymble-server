"""The one entry point for LLM calls (architecture §10.2).

`LLMClient.run()` routes a task to a provider, enforces a strict JSON schema (validated with
pydantic, one retry on invalid output), caches results in `llm_cache`, meters usage per
household in `llm_usage`, applies the monthly token cap, and falls back to the other
provider on errors and timeouts. When no provider can answer it returns a
`needs_review` result instead of raising, so callers degrade to "show it to the user".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from thymble.config import Settings
from thymble.llm.providers import (
    AnthropicProvider,
    ImageInput,
    LLMRequest,
    LLMResponse,
    OpenAICompatibleProvider,
    Provider,
    ProviderError,
    ProviderTimeout,
)
from thymble.llm.tasks import ANTHROPIC, LOCAL, TASK_SPECS, Task, normalize_route

log = logging.getLogger("thymble.llm")


@dataclass
class LLMResult:
    status: Literal["ok", "needs_review"]
    data: dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    cached: bool = False
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class UsageRecord:
    household_id: uuid.UUID | None
    task: str
    provider: str
    model: str
    status: str  # ok | invalid | error | timeout
    cached: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    cost_usd: Decimal | None = None


@dataclass
class CacheEntry:
    provider: str
    model: str
    result: dict[str, Any]


class LLMStore(Protocol):
    """Persistence for the cache and the meter (Postgres in production, memory in tests)."""

    def cache_get(self, key: str) -> CacheEntry | None: ...

    def cache_put(
        self, key: str, task: str, response: LLMResponse, provider: str, result: dict[str, Any]
    ) -> None: ...

    def record_usage(self, record: UsageRecord) -> None: ...

    def tokens_this_month(self, household_id: uuid.UUID) -> int: ...


@dataclass
class MemoryStore:
    cache: dict[str, CacheEntry] = field(default_factory=dict)
    usage: list[UsageRecord] = field(default_factory=list)

    def cache_get(self, key: str) -> CacheEntry | None:
        return self.cache.get(key)

    def cache_put(
        self, key: str, task: str, response: LLMResponse, provider: str, result: dict[str, Any]
    ) -> None:
        self.cache[key] = CacheEntry(provider, response.model, result)

    def record_usage(self, record: UsageRecord) -> None:
        self.usage.append(record)

    def tokens_this_month(self, household_id: uuid.UUID) -> int:
        return sum(
            (r.input_tokens or 0) + (r.output_tokens or 0)
            for r in self.usage
            if r.household_id == household_id and not r.cached
        )


class DbStore:
    """`llm_cache` + `llm_usage` through SQLAlchemy (sync; called from a worker thread)."""

    def __init__(self, sessionmaker: Any) -> None:
        self._sessionmaker = sessionmaker

    def cache_get(self, key: str) -> CacheEntry | None:
        from sqlalchemy import select

        from thymble.models import LlmCache

        with self._sessionmaker() as session:
            row = session.scalar(select(LlmCache).where(LlmCache.cache_key == key))
            if row is None:
                return None
            now = datetime.now(UTC)
            if row.expires_at is not None and row.expires_at < now:
                return None
            row.hit_count += 1
            row.last_hit_at = now
            session.commit()
            return CacheEntry(row.provider, row.model, row.result)

    def cache_put(
        self, key: str, task: str, response: LLMResponse, provider: str, result: dict[str, Any]
    ) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from thymble.models import LlmCache

        stmt = (
            insert(LlmCache)
            .values(
                id=uuid.uuid4(),
                cache_key=key,
                task=task,
                provider=provider,
                model=response.model,
                result=result,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
            )
            .on_conflict_do_nothing(index_elements=["cache_key"])
        )
        with self._sessionmaker() as session:
            session.execute(stmt)
            session.commit()

    def record_usage(self, record: UsageRecord) -> None:
        from thymble.models import LlmUsage

        with self._sessionmaker() as session:
            session.add(LlmUsage(**record.__dict__))
            session.commit()

    def tokens_this_month(self, household_id: uuid.UUID) -> int:
        from sqlalchemy import func, select

        from thymble.models import LlmUsage

        start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), 0)
        with self._sessionmaker() as session:
            return int(
                session.scalar(
                    select(total).where(
                        LlmUsage.household_id == household_id,
                        LlmUsage.cached.is_(False),
                        LlmUsage.created_at >= start,
                    )
                )
                or 0
            )


def normalize_prompt(text: str) -> str:
    """Cache-key normalization: Unicode NFKC and collapsed whitespace (case is kept:
    "1 T" and "1 t" are different units in old cookbooks)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema tightened for strict structured output: every object lists all
    of its properties as required and forbids extra keys; titles and defaults are dropped."""

    def tighten(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: tighten(v) for k, v in node.items() if k not in ("title", "default")}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [tighten(v) for v in node]
        return node

    return tighten(model.model_json_schema())


def cache_key(
    task: Task,
    schema_version: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    images: list[ImageInput],
) -> str:
    material = json.dumps(
        {
            "task": str(task),
            "v": schema_version,
            "system": normalize_prompt(system),
            "prompt": normalize_prompt(prompt),
            "schema": schema,
            "images": [hashlib.sha256(i.data).hexdigest() for i in images],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"{task}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def build_providers(settings: Settings) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    key = settings.anthropic_api_key.get_secret_value()
    if key:
        providers[ANTHROPIC] = AnthropicProvider(
            key, settings.anthropic_model_small, settings.anthropic_model_large
        )
    if settings.openai_compatible_base_url:
        providers[LOCAL] = OpenAICompatibleProvider(
            settings.openai_compatible_base_url,
            settings.local_model,
            settings.local_vision_model,
            settings.openai_compatible_api_key.get_secret_value(),
        )
    return providers


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        providers: dict[str, Provider] | None = None,
        store: LLMStore | None = None,
    ) -> None:
        self.settings = settings
        self.providers = build_providers(settings) if providers is None else providers
        self.store: LLMStore = store if store is not None else MemoryStore()

    @property
    def available(self) -> bool:
        return bool(self.providers)

    def route(self, task: Task) -> list[str]:
        """Providers to try for a task, in order: the routed one, then the fallback."""
        configured = getattr(self.settings, f"llm_route_{task.value.lower()}", "") or ""
        route = normalize_route(configured) if configured else TASK_SPECS[task].default_route
        if route == "none":
            return []
        order = [route]
        if self.settings.llm_fallback:
            order.append(LOCAL if route == ANTHROPIC else ANTHROPIC)
        return [name for name in order if name in self.providers]

    def _timeout(self, provider: str) -> float:
        if provider == LOCAL:
            return self.settings.llm_local_timeout_seconds
        return self.settings.llm_timeout_seconds

    async def _store(self, method: str, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(getattr(self.store, method), *args)
        except Exception:  # metering/caching must never break the import itself
            log.exception("llm store %s failed", method)
            return None

    async def run(
        self,
        task: Task,
        *,
        system: str,
        prompt: str,
        output: type[BaseModel],
        household_id: uuid.UUID | None = None,
        images: list[ImageInput] | None = None,
        schema_version: str = "1",
        use_cache: bool = True,
    ) -> LLMResult:
        images = images or []
        chain = self.route(task)
        if not chain:
            reason = (
                "no LLM provider configured" if not self.providers else f"LLM disabled for {task}"
            )
            return LLMResult("needs_review", reason=reason)

        schema = strict_schema(output)
        key = cache_key(task, schema_version, system, prompt, schema, images)
        if use_cache:
            hit = await self._store("cache_get", key)
            if hit is not None:
                try:
                    data = output.model_validate(hit.result).model_dump(mode="json")
                except ValidationError:
                    data = None  # schema changed without a version bump: ask again
                if data is not None:
                    await self._store(
                        "record_usage",
                        UsageRecord(household_id, task, hit.provider, hit.model, "ok", cached=True),
                    )
                    return LLMResult("ok", data, hit.provider, hit.model, cached=True)

        limit = self.settings.llm_household_monthly_token_limit
        if limit and household_id is not None:
            used = await self._store("tokens_this_month", household_id) or 0
            if used >= limit:
                return LLMResult("needs_review", reason="monthly LLM token limit reached")

        spec = TASK_SPECS[task]
        reason = "LLM call failed"
        for name in chain:
            provider = self.providers[name]
            request = LLMRequest(
                system=system,
                prompt=prompt,
                schema=schema,
                schema_name=f"{task}_{output.__name__}".lower(),
                max_tokens=spec.max_tokens,
                hard=spec.hard,
                images=images,
            )
            for attempt in (1, 2):  # one retry on invalid output
                try:
                    response = await provider.complete(request, self._timeout(name))
                except ProviderTimeout as exc:
                    reason = str(exc)
                    await self._store(
                        "record_usage", UsageRecord(household_id, task, name, "?", "timeout")
                    )
                    break
                except ProviderError as exc:
                    reason = str(exc)
                    await self._store(
                        "record_usage", UsageRecord(household_id, task, name, "?", "error")
                    )
                    break
                record = UsageRecord(
                    household_id,
                    task,
                    name,
                    response.model,
                    "ok",
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    latency_ms=response.latency_ms,
                    cost_usd=response.cost_usd,
                )
                try:
                    data = output.model_validate_json(response.text).model_dump(mode="json")
                except ValidationError as exc:
                    record.status = "invalid"
                    await self._store("record_usage", record)
                    reason = f"{name} returned output that does not match the schema"
                    log.info("%s %s attempt %d invalid: %s", task, name, attempt, exc)
                    request.prompt = (
                        f"{prompt}\n\nYour previous reply did not match the required JSON "
                        f"schema ({_short_errors(exc)}). Reply again with JSON only."
                    )
                    continue
                await self._store("record_usage", record)
                if use_cache:
                    await self._store("cache_put", key, str(task), response, name, data)
                return LLMResult("ok", data, name, response.model)
            log.info("%s: provider %s gave up (%s); trying fallback", task, name, reason)
        return LLMResult("needs_review", reason=reason)


def _short_errors(exc: ValidationError) -> str:
    parts = [f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in exc.errors()[:5]]
    return "; ".join(parts)


__all__ = [
    "DbStore",
    "LLMClient",
    "LLMResult",
    "MemoryStore",
    "UsageRecord",
    "build_providers",
    "strict_schema",
]
