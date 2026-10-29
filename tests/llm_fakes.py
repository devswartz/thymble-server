"""Test doubles for LLM providers (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable

from thymble.llm.providers import LLMRequest, LLMResponse


class FakeProvider:
    """Replies from a list (strings or exceptions), or from a function of the request."""

    def __init__(
        self, name: str, replies: list[object] | Callable[[LLMRequest], object] | None = None
    ) -> None:
        self.name = name
        self.replies = replies if callable(replies) else list(replies or [])
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest, timeout: float) -> LLMResponse:
        self.requests.append(LLMRequest(**{**request.__dict__, "images": list(request.images)}))
        reply = self.replies(request) if callable(self.replies) else self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if not isinstance(reply, str):
            reply = json.dumps(reply)
        return LLMResponse(text=reply, model=f"{self.name}-model", input_tokens=10, output_tokens=5)
