"""LLM providers: Anthropic (Claude) and any OpenAI-compatible endpoint (Ollama, vLLM ...).

A provider turns one `LLMRequest` into raw JSON text. Validation, retries, caching, metering
and fallback live in `thymble.llm.client`; providers only talk to their API.
"""

from __future__ import annotations

import asyncio
import base64
import time
import weakref
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import anthropic
import httpx


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    media_type: str  # image/jpeg, image/png, image/webp, image/gif


@dataclass
class LLMRequest:
    system: str
    prompt: str
    schema: dict[str, Any]
    schema_name: str
    max_tokens: int
    hard: bool = False
    images: list[ImageInput] = field(default_factory=list)


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None


class ProviderError(Exception):
    """The provider failed (HTTP error, refusal, unusable response)."""


class ProviderTimeout(ProviderError):
    """The provider did not answer in time (or the local queue was too long)."""


class Provider(Protocol):
    name: str

    async def complete(self, request: LLMRequest, timeout: float) -> LLMResponse: ...


# List prices in USD per million tokens (input, output), for spend estimates only.
ANTHROPIC_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
}


def estimate_cost(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    price = ANTHROPIC_PRICES.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    total = price[0] * input_tokens + price[1] * output_tokens
    return (total / Decimal(1_000_000)).quantize(Decimal("0.000001"))


class AnthropicProvider:
    """Claude through the official SDK, with structured (JSON-schema) output."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        small_model: str,
        large_model: str,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.small_model = small_model
        self.large_model = large_model
        # Retries are handled by our client (fallback to the other provider), not the SDK.
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=1)

    async def complete(self, request: LLMRequest, timeout: float) -> LLMResponse:
        model = self.large_model if request.hard else self.small_model
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.media_type,
                    "data": base64.standard_b64encode(image.data).decode("ascii"),
                },
            }
            for image in request.images
        ]
        content.append({"type": "text", "text": request.prompt})
        started = time.monotonic()
        try:
            message = await self._client.with_options(timeout=timeout).messages.create(
                model=model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": "medium" if request.hard else "low",
                    "format": {"type": "json_schema", "schema": request.schema},
                },
            )
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeout(f"anthropic timed out after {timeout:.0f}s") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"anthropic HTTP {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("anthropic connection error") from exc
        if message.stop_reason == "refusal":
            raise ProviderError("anthropic declined the request")
        if message.stop_reason == "max_tokens":
            raise ProviderError("anthropic reply hit max_tokens")
        text = next((block.text for block in message.content if block.type == "text"), None)
        if text is None:
            raise ProviderError("anthropic reply had no text block")
        usage = message.usage
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=estimate_cost(model, usage.input_tokens, usage.output_tokens),
            latency_ms=int((time.monotonic() - started) * 1000),
        )


# One local job at a time per event loop (D-019: the local GPU is shared with Frigate).
_local_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _local_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _local_locks.get(loop)
    if lock is None:
        lock = _local_locks[loop] = asyncio.Lock()
    return lock


class OpenAICompatibleProvider:
    """Any `/v1/chat/completions` endpoint: Ollama, llama.cpp server, vLLM, LM Studio.

    Calls are serialized (one at a time per worker process) and the timeout covers the
    wait for the lock, so a busy local model makes the client fall back instead of queuing.
    """

    name = "local"

    def __init__(
        self,
        base_url: str,
        model: str,
        vision_model: str = "",
        api_key: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.vision_model = vision_model or model
        self.api_key = api_key
        self._http = http_client

    async def complete(self, request: LLMRequest, timeout: float) -> LLMResponse:
        model = self.vision_model if request.images else self.model
        if not model:
            raise ProviderError("LOCAL_MODEL is not set")
        user_content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        for image in request.images:
            encoded = base64.standard_b64encode(image.data).decode("ascii")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.media_type};base64,{encoded}"},
                }
            )
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.schema,
                    "strict": True,
                },
            },
            "max_tokens": request.max_tokens,
            "temperature": 0,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout), _local_lock():
                payload = await self._post(body, headers, timeout)
        except TimeoutError as exc:
            raise ProviderTimeout(f"local model timed out after {timeout:.0f}s") from exc
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("local model reply had no message content") from exc
        if not isinstance(text, str):
            raise ProviderError("local model reply content was not text")
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def _post(self, body: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
        url = f"{self.base_url}/chat/completions"
        try:
            if self._http is not None:
                response = await self._http.post(url, json=body, headers=headers, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as http:
                    response = await http.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise TimeoutError from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"local model connection error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"local model HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("local model reply was not JSON") from exc
