"""LLM module: routing, strict schema, validation + retry, cache, metering, fallback.

Both providers are mocked; nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from tests.conftest import make_settings
from thymble.llm import ImageInput, LLMClient, MemoryStore, Task, strict_schema
from thymble.llm.providers import (
    AnthropicProvider,
    LLMRequest,
    LLMResponse,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderTimeout,
    estimate_cost,
)


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    quantity: float | None = None


class FakeProvider:
    def __init__(self, name: str, replies: list[object]) -> None:
        self.name = name
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest, timeout: float) -> LLMResponse:
        self.requests.append(
            LLMRequest(**{**request.__dict__, "images": list(request.images)})  # snapshot
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(text=reply, model=f"{self.name}-model", input_tokens=10, output_tokens=5)


def run(coro):
    return asyncio.run(coro)


def client(providers: dict, store: MemoryStore | None = None, **settings) -> LLMClient:
    return LLMClient(make_settings(**settings), providers=providers, store=store or MemoryStore())


def call(c: LLMClient, task: Task = Task.T3, prompt: str = "2 cups flour", **kw):
    return run(c.run(task, system="Parse.", prompt=prompt, output=Item, **kw))


def test_no_provider_returns_needs_review():
    result = call(client({}))
    assert result.status == "needs_review"
    assert result.reason == "no LLM provider configured"
    assert result.data is None


def test_strict_schema_requires_every_property_and_forbids_extras():
    schema = strict_schema(Item)
    assert schema["required"] == ["name", "quantity"]
    assert schema["additionalProperties"] is False
    assert "title" not in schema and "default" not in schema["properties"]["quantity"]


def test_default_routes_small_tasks_local_and_hard_tasks_anthropic():
    local, claude = FakeProvider("local", []), FakeProvider("anthropic", [])
    c = client({"local": local, "anthropic": claude})
    assert c.route(Task.T3) == ["local", "anthropic"]
    assert c.route(Task.T1) == ["anthropic", "local"]


def test_route_override_from_config_and_disable():
    providers = {"local": FakeProvider("local", []), "anthropic": FakeProvider("anthropic", [])}
    c = client(providers, llm_route_t3="anthropic", llm_route_t4="none")
    assert c.route(Task.T3) == ["anthropic", "local"]
    assert c.route(Task.T4) == []
    assert call(c, Task.T4).reason == "LLM disabled for T4"
    no_fallback = client(providers, llm_fallback=False)
    assert no_fallback.route(Task.T3) == ["local"]


def test_route_env_var(monkeypatch):
    monkeypatch.setenv("LLM_ROUTE_T1", "ollama")
    c = client({"local": FakeProvider("local", []), "anthropic": FakeProvider("anthropic", [])})
    assert c.route(Task.T1) == ["local", "anthropic"]


def test_missing_routed_provider_uses_the_other():
    claude = FakeProvider("anthropic", ['{"name": "flour", "quantity": 2}'])
    result = call(client({"anthropic": claude}))
    assert result.ok and result.provider == "anthropic"
    assert result.data == {"name": "flour", "quantity": 2.0}


def test_invalid_output_is_retried_once_with_the_error():
    local = FakeProvider("local", ['{"quantity": 2}', '{"name": "flour", "quantity": 2}'])
    store = MemoryStore()
    result = call(client({"local": local}, store))
    assert result.ok
    assert len(local.requests) == 2
    assert "did not match the required JSON schema" in local.requests[1].prompt
    assert [u.status for u in store.usage] == ["invalid", "ok"]


def test_two_invalid_replies_fall_back_to_the_other_provider():
    local = FakeProvider("local", ["not json", '{"nope": 1}'])
    claude = FakeProvider("anthropic", ['{"name": "flour", "quantity": null}'])
    result = call(client({"local": local, "anthropic": claude}))
    assert result.ok and result.provider == "anthropic"
    assert len(local.requests) == 2 and len(claude.requests) == 1


def test_timeout_falls_back_and_errors_end_in_needs_review():
    local = FakeProvider("local", [ProviderTimeout("local model timed out after 1s")])
    claude = FakeProvider("anthropic", [ProviderError("anthropic HTTP 500")])
    store = MemoryStore()
    result = call(client({"local": local, "anthropic": claude}, store))
    assert result.status == "needs_review"
    assert result.reason == "anthropic HTTP 500"
    assert [(u.provider, u.status) for u in store.usage] == [
        ("local", "timeout"),
        ("anthropic", "error"),
    ]


def test_cache_hit_skips_the_provider_and_is_metered_as_cached():
    household = uuid.uuid4()
    local = FakeProvider("local", ['{"name": "flour", "quantity": 2}'])
    store = MemoryStore()
    c = client({"local": local}, store)
    first = call(c, household_id=household)
    # Same prompt modulo whitespace -> same normalized cache key.
    second = call(c, prompt="  2 cups\n flour ", household_id=household)
    assert first.ok and not first.cached
    assert second.ok and second.cached and second.data == first.data
    assert len(local.requests) == 1
    assert [u.cached for u in store.usage] == [False, True]
    assert store.tokens_this_month(household) == 15


def test_case_is_part_of_the_cache_key():
    local = FakeProvider("local", ['{"name": "a"}', '{"name": "b"}'])
    c = client({"local": local})
    assert call(c, prompt="1 T sugar").data["name"] == "a"
    assert call(c, prompt="1 t sugar").data["name"] == "b"


def test_monthly_token_limit_blocks_uncached_calls():
    household = uuid.uuid4()
    local = FakeProvider("local", ['{"name": "flour"}', '{"name": "salt"}'])
    c = client({"local": local}, llm_household_monthly_token_limit=10)
    assert call(c, household_id=household).ok
    blocked = call(c, prompt="1 tsp salt", household_id=household)
    assert blocked.status == "needs_review"
    assert blocked.reason == "monthly LLM token limit reached"
    # Another household is unaffected.
    assert call(c, prompt="1 tsp salt", household_id=uuid.uuid4()).ok


# ------------------------------------------------------------------ Anthropic provider


class FakeMessages:
    def __init__(self, message=None, exc=None) -> None:
        self.message, self.exc, self.kwargs = message, exc, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.message


class FakeAnthropic:
    def __init__(self, messages: FakeMessages) -> None:
        self.messages = messages
        self.timeout = None

    def with_options(self, timeout):
        self.timeout = timeout
        return self


def anthropic_message(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
    )


def test_anthropic_provider_sends_schema_images_and_picks_model():
    messages = FakeMessages(anthropic_message('{"name": "x"}'))
    fake = FakeAnthropic(messages)
    provider = AnthropicProvider("k", "claude-haiku-5-5", "claude-sonnet-5-5", client=fake)
    request = LLMRequest(
        system="sys",
        prompt="hello",
        schema={"type": "object"},
        schema_name="t",
        max_tokens=100,
        hard=True,
        images=[ImageInput(b"\x89PNG", "image/png")],
    )
    response = run(provider.complete(request, timeout=12))
    sent = messages.kwargs
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert sent["messages"][0]["content"][0]["type"] == "image"
    assert sent["messages"][0]["content"][-1] == {"type": "text", "text": "hello"}
    assert fake.timeout == 12
    assert response.text == '{"name": "x"}'
    assert response.cost_usd == estimate_cost("claude-sonnet-5-5", 1000, 200)

    request.hard = False
    run(provider.complete(request, timeout=12))
    assert messages.kwargs["model"] == "claude-haiku-5-5"


def test_anthropic_refusal_is_a_provider_error():
    fake = FakeAnthropic(FakeMessages(anthropic_message("", stop_reason="refusal")))
    provider = AnthropicProvider("k", "s", "l", client=fake)
    request = LLMRequest("s", "p", {}, "t", 10)
    with pytest.raises(ProviderError, match="declined"):
        run(provider.complete(request, timeout=1))


# ------------------------------------------------------------------ OpenAI-compatible provider


def test_openai_compatible_provider_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"name": "flour"}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://ollama:11434/v1/", "gemma3:4b", "", api_key="secret", http_client=http
    )
    request = LLMRequest(
        "sys", "p", {"type": "object"}, "t3_item", 50, images=[ImageInput(b"img", "image/jpeg")]
    )
    response = run(provider.complete(request, timeout=5))
    assert seen["url"] == "http://ollama:11434/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    body = seen["body"]
    assert body["model"] == "gemma3:4b"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;")
    assert (response.text, response.input_tokens, response.output_tokens) == (
        '{"name": "flour"}',
        7,
        3,
    )


def test_openai_compatible_http_error_and_timeout():
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    provider = OpenAICompatibleProvider(
        "http://x/v1", "m", http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail))
    )
    with pytest.raises(ProviderError, match="HTTP 503"):
        run(provider.complete(LLMRequest("s", "p", {}, "t", 10), timeout=5))

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    provider = OpenAICompatibleProvider(
        "http://x/v1", "m", http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow))
    )
    with pytest.raises(ProviderTimeout):
        run(provider.complete(LLMRequest("s", "p", {}, "t", 10), timeout=5))


def test_local_calls_run_one_at_a_time():
    active, peak = 0, 0

    class SlowLocal(OpenAICompatibleProvider):
        async def _post(self, body, headers, timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"choices": [{"message": {"content": "{}"}}]}

    provider = SlowLocal("http://x/v1", "m")

    async def many():
        request = LLMRequest("s", "p", {}, "t", 10)
        await asyncio.gather(*(provider.complete(request, timeout=5) for _ in range(4)))

    run(many())
    assert peak == 1
