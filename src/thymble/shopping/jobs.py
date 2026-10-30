"""Worker jobs for shopping (arq)."""

from __future__ import annotations

import logging
from typing import Any

from thymble.db.session import get_sessionmaker
from thymble.shopping.trips import due_reminders

log = logging.getLogger("thymble.worker")


async def trip_reminders(ctx: dict[str, Any]) -> int:
    """Pickup orders past their pickup time -> "Got everything?" (D-030).

    Delivery of the reminder to devices (push) is not built yet: the trip's status
    (awaiting_confirmation) and `reminded_at` sync to the app, which shows the prompt.
    """
    with get_sessionmaker()() as session:
        trips = due_reminders(session)
        session.commit()
    for trip in trips:
        log.info("trip %s (%s) awaits confirmation; reminder due", trip.id, trip.kind.value)
    return len(trips)


async def start_connectors(ctx: dict[str, Any]) -> None:
    """Start the Home Assistant connector supervisor when the connector is enabled."""
    from thymble.config import get_settings
    from thymble.connectors.home_assistant.supervisor import ConnectorSupervisor

    settings = get_settings()
    if "home_assistant" not in settings.connectors_lists:
        return
    supervisor = ConnectorSupervisor(settings, get_sessionmaker(), redis=ctx.get("redis"))
    supervisor.start()
    ctx["connector_supervisor"] = supervisor
    log.info("Home Assistant connector supervisor started")


async def stop_connectors(ctx: dict[str, Any]) -> None:
    supervisor = ctx.pop("connector_supervisor", None)
    if supervisor is not None:
        await supervisor.stop()
