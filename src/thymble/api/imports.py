"""Recipe import API (architecture §5, D-025).

POST /v1/imports      start an import; returns 202 with the job id
    JSON {"html": "...", "final_url": "..."}  page captured on the device (preferred)
    JSON {"url": "..."}                       server-side fetch fallback
    JSON {"text": "..."}                      pasted recipe text
    multipart form, field "photo"             photo of a recipe card or cookbook page, plus
        optional "device_ocr_text" (the phone's own OCR of the photo, D-035) and
        "device_ocr_confidences" (JSON array, one 0..1 value per line of that text)
GET  /v1/imports/{id} status and result (the draft recipe id once done), with the
    confidence of every line, step and section (D-034)
POST /v1/imports/{id}/finalize  finish the review: the user's decisions per item (see
    thymble.importer.review); 409 while a "low" item is not confirmed or edited

Both need a bearer token and the household in X-Thymble-Household (thymble.auth.deps).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from thymble.auth.deps import CurrentHouseholdDep
from thymble.config import Settings, get_settings
from thymble.db.deps import get_db
from thymble.importer import review as review_service
from thymble.importer.jobs import JOB_NAME
from thymble.importer.pipeline import PHOTO_TYPES
from thymble.importer.readings import split_device_text

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
    # Import review (D-034): {"state": needs_review | reviewed, "counts": {high, check, low},
    # "sections": {...}}; per-line and per-step confidence is in result.ingredients / steps.
    review: dict[str, Any] | None
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
        review=(job.result or {}).get("review"),
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
                    "properties": {
                        "photo": {"type": "string", "format": "binary"},
                        "device_ocr_text": {
                            "type": "string",
                            "description": "The device's own OCR of the photo (D-035)",
                        },
                        "device_ocr_confidences": {
                            "type": "string",
                            "description": "JSON array: one 0..1 confidence per line of "
                            "device_ocr_text",
                        },
                    },
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
    job = ImportJob(household_id=hh.household_id, created_by=hh.user_id, status=ImportStatus.QUEUED)
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
        job.input = _device_reading(form.get("device_ocr_text"), form.get("device_ocr_confidences"))
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


MAX_DEVICE_OCR_CHARS = 100_000


def _device_reading(text: Any, confidences: Any) -> dict[str, Any] | None:
    """The phone's own OCR of the photo (D-035), checked and stored with the job."""
    if text is None or text == "":
        if confidences not in (None, ""):
            raise HTTPException(422, "device_ocr_confidences needs device_ocr_text")
        return None
    if not isinstance(text, str):
        raise HTTPException(422, "device_ocr_text must be a text field")
    if len(text) > MAX_DEVICE_OCR_CHARS:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "device_ocr_text too long")
    scores: list[float] | None = None
    if confidences not in (None, ""):
        try:
            scores = json.loads(str(confidences))
        except ValueError as exc:
            raise HTTPException(422, "device_ocr_confidences must be a JSON array") from exc
        if not isinstance(scores, list) or not all(
            isinstance(x, int | float) and not isinstance(x, bool) and 0 <= x <= 1 for x in scores
        ):
            raise HTTPException(422, "device_ocr_confidences must be numbers from 0 to 1")
        try:
            split_device_text(text, [float(x) for x in scores])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"device_ocr_text": text, "device_ocr_confidences": scores}


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


# ------------------------------------------------------------------------------ review


class IngredientEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: float | None = Field(default=None, ge=0)
    quantity_max: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=32)
    name: str | None = None
    additional: list[str] = []
    alternatives: list[str] = []
    preparation: str | None = None
    note: str | None = None
    optional: bool = False


class TimesValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prep_minutes: int | None = Field(default=None, ge=0)
    cook_minutes: int | None = Field(default=None, ge=0)
    total_minutes: int | None = Field(default=None, ge=0)


class YieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = None
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=64)


class ReviewDecision(BaseModel):
    """One item the user decided. Ingredients and steps by `index` in the import result,
    sections by `field`. Edits carry `ingredient`, `text` (step) or `value` (section)."""

    model_config = ConfigDict(extra="forbid")
    target: Literal["ingredient", "step", "section"]
    index: int | None = Field(default=None, ge=0)
    field: Literal["title", "yield", "times", "groups"] | None = None
    action: Literal["confirmed", "accepted", "edited"]
    confidence: Literal["high", "check", "low"] | None = None
    ingredient: IngredientEdit | None = None
    text: str | None = Field(default=None, min_length=1)
    value: Any = None

    @model_validator(mode="after")
    def _shape(self) -> ReviewDecision:
        section = self.target == "section"
        if section != (self.field is not None) or section == (self.index is not None):
            raise ValueError("ingredients and steps take an index, sections a field")
        others = [k for k in ("ingredient", "text", "value") if k in self.model_fields_set]
        expected = {"ingredient": "ingredient", "step": "text", "section": "value"}[self.target]
        if self.action != "edited":
            if others:
                raise ValueError(f"{self.action} takes no {others[0]}")
            return self
        if others != [expected] or (expected != "value" and getattr(self, expected) is None):
            raise ValueError(f"an edited {self.target} needs `{expected}` (and only that)")
        if section:
            self.value = _section_value(self.field, self.value)
        return self


def _section_value(name: str | None, value: Any) -> Any:
    if name == "title":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("the title must be non-empty text")
        return value.strip()
    if name == "yield":
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            return YieldValue.model_validate(value).model_dump()
        raise ValueError('yield is text ("Serves 4") or {text, quantity, unit}')
    if name == "times":
        if not isinstance(value, dict):
            raise ValueError("times is {prep_minutes, cook_minutes, total_minutes}")
        times = TimesValue.model_validate(value)
        return times.model_dump(include=times.model_fields_set)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("groups is a list of group names")
    return sorted(value)


class FinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[ReviewDecision] = []


def _decision(d: ReviewDecision) -> review_service.Decision:
    return review_service.Decision(
        target=d.target,
        key=d.field if d.target == "section" else d.index,  # type: ignore[arg-type]
        action=d.action,
        confidence=d.confidence,
        ingredient=d.ingredient.model_dump(include=d.ingredient.model_fields_set)
        if d.ingredient
        else {},
        text=d.text,
        value=d.value,
    )


@router.post("/imports/{import_id}/finalize", response_model=ImportStatusOut)
def finalize_import(
    import_id: uuid.UUID,
    body: FinalizeRequest,
    db: Annotated[Session, Depends(get_db)],
    hh: CurrentHouseholdDep,
) -> ImportStatusOut:
    from thymble.models import ImportJob, ImportStatus

    job = db.get(ImportJob, import_id, with_for_update=True)
    if job is None or job.household_id != hh.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")
    if job.status != ImportStatus.DONE or job.recipe_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this import has no recipe to finalize")
    try:
        outcome = review_service.finalize(
            db,
            household_id=hh.household_id,
            user_id=hh.user_id,
            recipe_id=job.recipe_id,
            result=job.result or {},
            import_id=job.id,
            decisions=[_decision(d) for d in body.decisions],
        )
    except review_service.ReviewError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
    result = dict(job.result or {})
    result["review"] = {
        **(result.get("review") or {}),
        "state": "reviewed",
        "outcomes": outcome["outcomes"],
    }
    result["needs_review"] = False
    job.result = result
    job.needs_review = False
    db.commit()
    db.refresh(job)
    return _out(job)
