"""Recipe import API (architecture §5, D-025).

POST /v1/imports      start an import; returns 202 with the job id
    JSON {"html": "...", "final_url": "..."}  page captured on the device (preferred)
    JSON {"url": "..."}                       server-side fetch fallback
    JSON {"text": "..."}                      pasted recipe text
    multipart form, field "photo"             photo of a recipe card or cookbook page
GET  /v1/imports/{id} status and result (the draft recipe id once done)

Both need a bearer token and the household in X-Thymble-Household (thymble.auth.deps).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from sqlalchemy.orm import Session

from thymble.auth.deps import CurrentHouseholdDep
from thymble.config import Settings, get_settings
from thymble.db.deps import get_db
from thymble.importer.jobs import JOB_NAME
from thymble.importer.pipeline import PHOTO_TYPES

router = APIRouter(tags=["imports"])

Enqueue = Callable[[str], Awaitable[None]]


def get_enqueue(settings: Annotated[Settings, Depends(get_settings)]) -> Enqueue:
    async def enqueue(job_id: str) -> None:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        try:
            await pool.enqueue_job(JOB_NAME, job_id, _job_id=f"import:{job_id}")
        finally:
            await pool.aclose()

    return enqueue


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html: str | None = None
    final_url: str | None = None
    url: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> ImportRequest:
        given = [k for k in ("html", "url", "text") if getattr(self, k)]
        if len(given) != 1:
            raise ValueError("send exactly one of html, url or text (or a photo as multipart)")
        if self.html and not self.final_url:
            raise ValueError("final_url is required with html")
        for name in ("url", "final_url"):
            value = getattr(self, name)
            if value and not value.lower().startswith(("http://", "https://")):
                raise ValueError(f"{name} must be an http(s) URL")
        return self


class ImportStatusOut(BaseModel):
    id: uuid.UUID
    status: str
    source_kind: str
    requested_url: str | None
    final_url: str | None
    recipe_id: uuid.UUID | None
    needs_review: bool
    error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None


def _out(job: Any) -> ImportStatusOut:
    return ImportStatusOut(
        id=job.id,
        status=str(job.status),
        source_kind=str(job.source_kind),
        requested_url=job.requested_url,
        final_url=job.final_url,
        recipe_id=job.recipe_id,
        needs_review=job.needs_review,
        error=job.error,
        result=job.result,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": ImportRequest.model_json_schema()},
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"photo": {"type": "string", "format": "binary"}},
                    "required": ["photo"],
                }
            },
        },
    }
}


@router.post(
    "/imports",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ImportStatusOut,
    openapi_extra=_REQUEST_BODY,
)
async def create_import(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[Session, Depends(get_db)],
    hh: CurrentHouseholdDep,
    enqueue: Annotated[Enqueue, Depends(get_enqueue)],
) -> ImportStatusOut:
    from thymble.models import ImportJob, ImportSourceKind, ImportStatus

    content_type = request.headers.get("content-type", "")
    job = ImportJob(household_id=hh.household_id, status=ImportStatus.QUEUED)
    if content_type.startswith("multipart/form-data"):
        if not (settings.llm_providers and settings.photo_import_enabled):
            raise HTTPException(status.HTTP_409_CONFLICT, "photo import is not available")
        form = await request.form()
        photo = form.get("photo")
        if photo is None or isinstance(photo, str):
            raise HTTPException(422, "photo file required")
        mime = (photo.content_type or "").split(";")[0].strip().lower()
        if mime not in PHOTO_TYPES:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "photo must be JPEG, PNG, WebP or GIF"
            )
        data = await photo.read(settings.import_max_photo_bytes + 1)
        if len(data) > settings.import_max_photo_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "photo too large")
        job.source_kind, job.photo, job.photo_mime = ImportSourceKind.PHOTO, data, mime
    else:
        try:
            body = ImportRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            raise RequestValidationError(
                exc.errors(include_url=False, include_context=False)
            ) from exc
        if body.html is not None:
            if len(body.html.encode("utf-8")) > settings.import_max_html_bytes:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "page too large")
            job.source_kind, job.final_url = ImportSourceKind.HTML, body.final_url
            job.input = {"html": body.html}
        elif body.url is not None:
            if not settings.import_server_fetch_enabled:
                raise HTTPException(status.HTTP_409_CONFLICT, "server-side fetching is off")
            job.source_kind, job.requested_url = ImportSourceKind.URL, body.url
        else:
            job.source_kind, job.input = ImportSourceKind.TEXT, {"text": body.text}
    db.add(job)
    db.commit()
    db.refresh(job)
    await enqueue(str(job.id))
    return _out(job)


@router.get("/imports/{import_id}", response_model=ImportStatusOut)
def get_import(
    import_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    hh: CurrentHouseholdDep,
) -> ImportStatusOut:
    from thymble.models import ImportJob

    job = db.get(ImportJob, import_id)
    if job is None or job.household_id != hh.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")
    return _out(job)
