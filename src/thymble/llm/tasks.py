"""LLM tasks (architecture §10.1) and their default routing (D-017, D-019)."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Task(enum.StrEnum):
    T1 = "T1"  # messy page -> recipe JSON
    T2 = "T2"  # photo / OCR text -> recipe JSON (transcription and structuring passes)
    T3 = "T3"  # ingredient line parse fallback
    T4 = "T4"  # ingredient normalization / aliasing
    T5 = "T5"  # free-text voice item -> structured item
    T6 = "T6"  # ingredient -> retailer product pick
    T7 = "T7"  # item -> aisle category
    T8 = "T8"  # step timer detection fallback


ANTHROPIC = "anthropic"
LOCAL = "local"
# Accepted spellings for the OpenAI-compatible provider in LLM_ROUTE_* settings.
ROUTE_ALIASES = {
    "anthropic": ANTHROPIC,
    "claude": ANTHROPIC,
    "local": LOCAL,
    "openai_compatible": LOCAL,
    "openai-compatible": LOCAL,
    "ollama": LOCAL,
    "none": "none",
    "off": "none",
}


@dataclass(frozen=True)
class TaskSpec:
    default_route: str
    # Hard tasks use the large Anthropic model and more effort; small tasks the small one.
    hard: bool
    max_tokens: int


# Small tasks default to the local model, hard tasks to Claude (D-017). Every route can be
# changed with LLM_ROUTE_T<n>; the other configured provider is the fallback.
TASK_SPECS: dict[Task, TaskSpec] = {
    Task.T1: TaskSpec(ANTHROPIC, hard=True, max_tokens=8000),
    Task.T2: TaskSpec(ANTHROPIC, hard=True, max_tokens=8000),
    Task.T3: TaskSpec(LOCAL, hard=False, max_tokens=1000),
    Task.T4: TaskSpec(LOCAL, hard=False, max_tokens=500),
    Task.T5: TaskSpec(LOCAL, hard=False, max_tokens=500),
    Task.T6: TaskSpec(LOCAL, hard=False, max_tokens=1000),
    Task.T7: TaskSpec(LOCAL, hard=False, max_tokens=300),
    Task.T8: TaskSpec(LOCAL, hard=False, max_tokens=500),
}


def normalize_route(value: str) -> str:
    route = ROUTE_ALIASES.get(value.strip().lower())
    if route is None:
        raise ValueError(f"unknown LLM route {value!r}; use anthropic, local or none")
    return route
