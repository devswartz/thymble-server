"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from thymble import __version__
from thymble.api import capabilities, health
from thymble.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="Thymble Server",
        version=__version__,
        license_info={"name": "AGPL-3.0", "identifier": "AGPL-3.0-only"},
    )
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings
    app.include_router(health.router)
    app.include_router(capabilities.router, prefix="/v1")
    return app


app = create_app()
