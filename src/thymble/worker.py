"""Background worker (arq on Redis).

Run with: ``arq thymble.worker.WorkerSettings``
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from thymble.config import get_settings
from thymble.importer.jobs import import_recipe, warm_up
from thymble.shopping.jobs import start_connectors, stop_connectors, trip_reminders

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
    await start_connectors(ctx)


async def shutdown(ctx: dict[str, Any]) -> None:
    await stop_connectors(ctx)


class WorkerSettings:
    functions: ClassVar = [noop, import_recipe, trip_reminders]
    cron_jobs: ClassVar = [cron(trip_reminders, minute=set(range(0, 60, 5)), run_at_startup=True)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
