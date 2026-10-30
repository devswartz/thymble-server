"""Rate limiter: limit parsing, both counter stores, enforcement and client addresses.

No Redis needed: the Redis store runs against a small in-memory stand-in that implements
the commands it uses (INCR, EXPIRE NX, TTL) with the same semantics.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from tests.conftest import make_settings
from thymble.ratelimit import (
    KEY_PREFIX,
    Hit,
    Limit,
    MemoryRateLimiter,
    RedisRateLimiter,
    client_ip,
    enforce,
    get_rate_limiter,
    parse_limit,
)

# --- parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("5/hour", Limit(5, 3600)),
        ("5/HOUR", Limit(5, 3600)),
        (" 20 / 10min ", Limit(20, 600)),
        ("3/s", Limit(3, 1)),
        ("100/86400", Limit(100, 86400)),
        ("7/2days", Limit(7, 172800)),
        ("1/minute", Limit(1, 60)),
    ],
)
def test_parse_limit(spec, expected):
    assert parse_limit(spec) == expected


@pytest.mark.parametrize("spec", ["", "0", "off", "none", "0/hour", "  "])
def test_parse_limit_off(spec):
    assert parse_limit(spec) is None


@pytest.mark.parametrize("spec", ["five/hour", "5", "5/fortnight", "5/0", "5/0min", "-1/hour"])
def test_parse_limit_rejects_garbage(spec):
    with pytest.raises(ValueError):
        parse_limit(spec)


def test_settings_reject_bad_limit():
    with pytest.raises(ValidationError):
        make_settings(rate_limit_magic_link_per_email="lots")
    assert make_settings(rate_limit_invite_join_per_ip="0").rate_limit_invite_join_per_ip == "0"


# --- memory store ----------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_memory_window_counts_and_resets():
    clock = Clock()
    limiter = MemoryRateLimiter(clock)
    limit = Limit(3, 60)
    assert [limiter.hit("k", limit) for _ in range(4)] == [(1, 60), (2, 60), (3, 60), (4, 60)]
    clock.t += 45
    assert limiter.hit("k", limit) == (5, 15)
    clock.t += 15  # window over: a fresh one starts
    assert limiter.hit("k", limit) == (1, 60)


def test_memory_keys_are_independent():
    limiter = MemoryRateLimiter(Clock())
    limit = Limit(1, 60)
    assert limiter.hit("a", limit)[0] == 1
    assert limiter.hit("b", limit)[0] == 1
    assert limiter.hit("a", limit)[0] == 2


def test_memory_store_prunes_expired_windows():
    clock = Clock()
    limiter = MemoryRateLimiter(clock)
    for i in range(10_001):
        limiter.hit(f"k{i}", Limit(1, 10))
    clock.t += 11
    limiter.hit("fresh", Limit(1, 10))
    assert len(limiter._windows) == 1


# --- redis store -----------------------------------------------------------------------


class FakeRedis:
    """INCR / EXPIRE (with NX) / TTL over a dict, with a controllable clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.values: dict[str, int] = {}
        self.expiry: dict[str, float] = {}

    def _purge(self, key: str) -> None:
        if key in self.expiry and self.expiry[key] <= self.clock():
            self.values.pop(key, None)
            self.expiry.pop(key, None)

    def incr(self, key: str) -> int:
        self._purge(key)
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key: str, seconds: int, nx: bool = False) -> bool:
        self._purge(key)
        if key not in self.values or (nx and key in self.expiry):
            return False
        self.expiry[key] = self.clock() + seconds
        return True

    def ttl(self, key: str) -> int:
        self._purge(key)
        if key not in self.values:
            return -2
        if key not in self.expiry:
            return -1
        return int(self.expiry[key] - self.clock() + 0.999)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        assert transaction
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def queue(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return queue

    def execute(self) -> list[Any]:
        return [getattr(self.redis, n)(*a, **kw) for n, a, kw in self.calls]


def test_redis_window_counts_and_resets():
    clock = Clock()
    redis = FakeRedis(clock)
    limiter = RedisRateLimiter(redis)
    limit = Limit(2, 60)
    assert limiter.hit("k", limit) == (1, 60)
    clock.t += 20
    # EXPIRE NX: later hits don't push the window out.
    assert limiter.hit("k", limit) == (2, 40)
    assert limiter.hit("k", limit) == (3, 40)
    clock.t += 40
    assert limiter.hit("k", limit) == (1, 60)


def test_redis_missing_ttl_falls_back_to_period():
    class NoTtl(FakeRedis):
        def ttl(self, key: str) -> int:
            return -1

    limiter = RedisRateLimiter(NoTtl(Clock()))
    assert limiter.hit("k", Limit(1, 90)) == (1, 90)


# --- enforce ---------------------------------------------------------------------------


def test_enforce_raises_429_with_retry_after():
    limiter = MemoryRateLimiter(Clock())
    hits = [Hit("r", Limit(2, 60), "who")]
    enforce(limiter, hits, "slow down")
    enforce(limiter, hits, "slow down")
    with pytest.raises(HTTPException) as exc:
        enforce(limiter, hits, "slow down")
    assert exc.value.status_code == 429
    assert exc.value.detail == "slow down"
    assert exc.value.headers == {"Retry-After": "60"}


def test_enforce_counts_every_rule_and_reports_longest_wait():
    clock = Clock()
    limiter = MemoryRateLimiter(clock)
    short = Hit("short", Limit(1, 10), "x")
    long = Hit("long", Limit(1, 100), "x")
    enforce(limiter, [short, long], "no")
    with pytest.raises(HTTPException) as exc:
        enforce(limiter, [short, long], "no")
    assert exc.value.headers["Retry-After"] == "100"
    # The second rule was counted even though the first one tripped.
    assert limiter.hit(f"{KEY_PREFIX}:long:100:" + _digest("x"), Limit(1, 100))[0] == 3


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:32]


def test_enforce_skips_rules_that_are_off_or_have_no_identity():
    limiter = MemoryRateLimiter(Clock())
    for _ in range(5):
        enforce(limiter, [Hit("off", None, "x"), Hit("anon", Limit(1, 60), None)], "no")
    assert limiter._windows == {}


def test_enforce_without_limiter_allows_everything():
    for _ in range(5):
        enforce(None, [Hit("r", Limit(1, 60), "x")], "no")


def test_enforce_fails_open_when_store_is_down(caplog):
    class Down:
        def hit(self, key: str, limit: Limit) -> tuple[int, int]:
            raise ConnectionError("redis is down")

    for _ in range(3):
        enforce(Down(), [Hit("r", Limit(1, 60), "x")], "no")
    assert "rate limiter unavailable" in caplog.text


def test_keys_hash_identities():
    limiter = MemoryRateLimiter(Clock())
    enforce(limiter, [Hit("magic_link_email", Limit(5, 3600), "cook@example.com")], "no")
    (key,) = limiter._windows
    assert "cook@example.com" not in key
    assert key == f"{KEY_PREFIX}:magic_link_email:3600:{_digest('cook@example.com')}"


# --- store selection and client address ------------------------------------------------


def test_get_rate_limiter_follows_settings():
    assert get_rate_limiter(make_settings(rate_limit_enabled=False)) is None
    memory = get_rate_limiter(make_settings(rate_limit_storage="memory"))
    assert isinstance(memory, MemoryRateLimiter)
    assert get_rate_limiter(make_settings(rate_limit_storage="memory")) is memory
    redis = get_rate_limiter(make_settings(redis_url="redis://127.0.0.1:1/0"))
    assert isinstance(redis, RedisRateLimiter)


def test_unreachable_redis_fails_open():
    limiter = get_rate_limiter(make_settings(redis_url="redis://127.0.0.1:1/0"))
    for _ in range(3):
        enforce(limiter, [Hit("r", Limit(1, 60), "x")], "no")


def _request(headers: dict[str, str], peer: str | None = "10.0.0.9") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


def test_client_ip_uses_peer_by_default():
    settings = make_settings()
    assert client_ip(_request({"CF-Connecting-IP": "203.0.113.7"}), settings) == "10.0.0.9"
    assert client_ip(_request({}, peer=None), settings) is None


def test_client_ip_trusts_configured_header():
    settings = make_settings(client_ip_header="CF-Connecting-IP")
    assert client_ip(_request({"CF-Connecting-IP": "203.0.113.7"}), settings) == "203.0.113.7"
    xff = make_settings(client_ip_header="X-Forwarded-For")
    assert client_ip(_request({"X-Forwarded-For": "198.51.100.1, 10.0.0.2"}), xff) == (
        "198.51.100.1"
    )
    # Header missing: fall back to the peer.
    assert client_ip(_request({}), settings) == "10.0.0.9"
