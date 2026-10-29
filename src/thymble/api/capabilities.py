"""Capability report (architecture §3.3).

The app turns features on from this report, never from a hard-coded tier (D-008).
Every value is derived from configuration so self-hosters get an accurate picture.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from thymble import __version__
from thymble.config import Settings, get_settings

API_VERSION = "1.0"

router = APIRouter(tags=["capabilities"])


class Connectors(BaseModel):
    lists: list[str]
    retailers: list[str]


class Capabilities(BaseModel):
    api_version: str
    server_version: str
    llm: bool
    photo_import: bool
    email_sign_in: bool
    connectors: Connectors
    shared_learning: bool


@router.get("/capabilities")
def get_capabilities(settings: Annotated[Settings, Depends(get_settings)]) -> Capabilities:
    llm = bool(settings.llm_providers)
    return Capabilities(
        api_version=API_VERSION,
        server_version=__version__,
        llm=llm,
        # Photo import needs a model to transcribe and structure the photo (D-019).
        photo_import=llm and settings.photo_import_enabled,
        email_sign_in=settings.email_sign_in,
        connectors=Connectors(
            lists=settings.connectors_lists,
            retailers=settings.connectors_retailers,
        ),
        shared_learning=settings.shared_learning,
    )
