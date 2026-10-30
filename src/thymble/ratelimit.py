"""Request rate limits for abuse-prone endpoints (magic-link requests, invite joins).

Fixed-window counters, one per (rule, identity): the first hit in a window starts it, every
hit counts, and once a counter passes its limit the request gets 429 with `Retry-After`
set to the seconds left in the window. Identities (email addresses, client IPs, user ids)
are hashed before they become keys, so the counter store never holds them in the clear.

Two stores:
- `RedisRateLimiter` (production): INCR + EXPIRE NX in one transaction, so every api
  process shares the counters. If Redis is unreachable the limiter fails open and logs:
  a Redis outage should not stop people signing in.
- `MemoryRateLimiter`: per-process; for tests and single-process setups without Redis.

Limits are configured as strings such as "5/hour", "30/10min" or "100/86400"; "0" or an
empty string turns a rule off (see `parse_limit` and the RATE_LIMIT_* settings).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Protocol

from fastapi import Depends, HTTPException, Request, status

from thymble.config import Settings, get_settings

log = logging.getLogger(__name__)

KEY_PREFIX = "thymble:ratelimit"

_UNITS = {
    "": 1,
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}
_SPEC = re.compile(r"^\s*(\d+)\s*/\s*(\d*)\s*([a-z]*)\s*$")


@dataclass(frozen=True)
class Limit:
    count: int
    period_seconds: int

    def __str__(self) -> str:
        return f"{self.count}/{self.period_seconds}s"


def parse_limit(spec: str) -> Limit | None:
    """ "5/hour", "20/10min", "100/86400" -> Limit; "" or "0" -> None (rule off)."""
    spec = spec.strip().lower()
    if spec in ("", "0", "off", "none"):
        return None
    match = _SPEC.match(spec)
    if match is None or match.group(3) not in _UNITS:
        raise ValueError(f"invalid rate limit {spec!r}; expected e.g. '5/hour' or '20/600'")
    count = int(match.group(1))
    multiplier = int(match.group(2)) if match.group(2) else 1
    period = multiplier * _UNITS[match.group(3)]
    if count == 0:
        return None
    if period <= 0:
        raise ValueError(f"invalid rate limit {spec!r}: the period must be positive")
    return Limit(count, period)


@dataclass(frozen=True)
class Hit:
    """One counter to bump: the rule's name, its limit, and who it is counting."""

    rule: str
    limit: Limit | None
    identity: str | None


def _key(hit: Hit) -> str:
    assert hit.limit is not None and hit.identity is not None
    digest = hashlib.sha256(hit.identity.encode()).hexdigest()[:32]
    return f"{KEY_PREFIX}:{hit.rule}:{hit.limit.period_seconds}:{digest}"


class RateLimiter(Protocol):
    def hit(self, key: str, limit: Limit) -> tuple[int, int]:
        """Count one request. Returns (count in this window, seconds until it resets)."""
        ...


class MemoryRateLimiter:
    """In-process fixed windows. Thread-safe; `clock` is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, float]] = {}

    def hit(self, key: str, limit: Limit) -> tuple[int, int]:
        current = self._clock()
        with self._lock:
            count, resets_at = self._windows.get(key, (0, 0.0))
            if resets_at <= current:
                count, resets_at = 0, current + limit.period_seconds
            count += 1
            self._windows[key] = (count, resets_at)
            if len(self._windows) > 10_000:
                self._windows = {k: v for k, v in self._windows.items() if v[1] > current}
        return count, max(1, int(resets_at - current + 0.999))


class RedisRateLimiter:
    """Fixed windows in Redis, shared by every api process."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def hit(self, key: str, limit: Limit) -> tuple[int, int]:
        pipe = self._client.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, limit.period_seconds, nx=True)
        pipe.ttl(key)
        count, _, ttl = pipe.execute()
        return int(count), int(ttl) if ttl and int(ttl) > 0 else limit.period_seconds


@lru_cache
def _redis_limiter(url: str) -> RedisRateLimiter:
    import redis

    client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return RedisRateLimiter(client)


def get_rate_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RateLimiter | None:
    """The configured store, or None when rate limiting is off. Tests override this."""
    if not settings.rate_limit_enabled:
        return None
    if settings.rate_limit_storage == "memory":
        return _memory_limiter()
    return _redis_limiter(settings.redis_url)


@lru_cache
def _memory_limiter() -> MemoryRateLimiter:
    return MemoryRateLimiter()


Limiter = Annotated[RateLimiter | None, Depends(get_rate_limiter)]


def client_ip(request: Request, settings: Settings) -> str | None:
    """The caller's address.

    With CLIENT_IP_HEADER set (e.g. `CF-Connecting-IP` behind Cloudflare Tunnel) that header
    wins; only set it when every request reaches the api through a proxy that overwrites
    it. Otherwise the socket peer, which uvicorn's `--proxy-headers` already resolves from
    X-Forwarded-For for trusted proxies (FORWARDED_ALLOW_IPS).
    """
    if settings.client_ip_header:
        value = request.headers.get(settings.client_ip_header)
        if value:
            return value.split(",", 1)[0].strip() or None
    return request.client.host if request.client else None


def enforce(limiter: RateLimiter | None, hits: Iterable[Hit], message: str) -> None:
    """Count the request against every rule; raise 429 if any of them is over its limit.

    Every rule is counted even when an earlier one already trips, so a client can't
    dodge one counter by hammering another.
    """
    if limiter is None:
        return
    retry_after = 0
    for hit in hits:
        if hit.limit is None or not hit.identity:
            continue
        try:
            count, resets_in = limiter.hit(_key(hit), hit.limit)
        except Exception:
            log.warning("rate limiter unavailable; allowing request", exc_info=True)
            return
        if count > hit.limit.count:
            retry_after = max(retry_after, resets_in)
    if retry_after:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            message,
            headers={"Retry-After": str(retry_after)},
        )
