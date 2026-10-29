"""Background worker (arq on Redis).

Run with: ``arq thymble.worker.WorkerSettings``
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq.connections import RedisSettings

from thymble.config import get_settings
from thymble.importer.jobs import import_recipe, warm_up

log = logging.getLogger("thymble.worker")


async def noop(ctx: dict[str, Any], note: str = "") -> str:
    """Placeholder job that proves the queue round-trips."""
    log.info("noop job ran (job_id=%s note=%r)", ctx.get("job_id"), note)
    return "ok"


async def startup(ctx: dict[str, Any]) -> None:
    # arq configures logging for its own loggers only; give ours the same treatment.
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))
        log.addHandler(handler)
        log.propagate = False
    log.setLevel(get_settings().log_level.upper())
    await warm_up(ctx)
    log.info("worker started")


class WorkerSettings:
    functions: ClassVar = [noop, import_recipe]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
