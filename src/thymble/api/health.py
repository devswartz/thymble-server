from fastapi import APIRouter
from pydantic import BaseModel

from thymble import __version__

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    version: str


@router.get("/healthz")
def healthz() -> Health:
    """Liveness probe: the process is up and serving requests."""
    return Health(status="ok", version=__version__)
