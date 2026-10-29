"""arq job that runs an import and saves the draft recipe (architecture §5)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from thymble.config import get_settings
from thymble.importer.ingredients import load_parser
from thymble.importer.normalize import IngredientResolver
from thymble.importer.pipeline import ImportFailed, ImportInput, ImportOutcome, ImportPipeline
from thymble.llm import get_llm_client

log = logging.getLogger("thymble.importer")

JOB_NAME = "import_recipe"


def _dec(value: float | None, places: int = 4) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, places)))


def _clip(value: str | None, size: int) -> str | None:
    return value[:size] if value else value


def save_draft(session: Any, job: Any, outcome: ImportOutcome) -> uuid.UUID:
    """Write the outcome as a draft recipe (is_draft=True) with its lines and steps."""
    from thymble.models import Recipe, RecipeIngredient, RecipeSourceType, RecipeStep

    source_type = {
        "html": RecipeSourceType.URL,
        "url": RecipeSourceType.URL,
        "text": RecipeSourceType.TEXT,
        "photo": RecipeSourceType.PHOTO,
    }[str(job.source_kind)]
    d = outcome.draft
    y = outcome.yield_info
    recipe = Recipe(
        household_id=job.household_id,
        title=d.title or "Untitled recipe",
        description=d.description,
        source_url=outcome.final_url,
        source_type=source_type,
        yield_text=y.get("text"),
        yield_quantity=_dec(y.get("quantity"), 3),
        yield_unit=_clip(y.get("unit"), 64),
        prep_time_minutes=d.prep_time_minutes,
        cook_time_minutes=d.cook_time_minutes,
        total_time_minutes=d.total_time_minutes,
        image_url=d.image_url,
        created_by=job.created_by,
        is_draft=True,
    )
    session.add(recipe)
    session.flush()
    for position, line in enumerate(outcome.lines):
        session.add(
            RecipeIngredient(
                household_id=job.household_id,
                recipe_id=recipe.id,
                position=position,
                raw_text=line.raw_text,
                quantity=_dec(line.quantity),
                quantity_max=_dec(line.quantity_max),
                unit=_clip(line.unit, 32),
                ingredient_id=uuid.UUID(line.ingredient_id) if line.ingredient_id else None,
                preparation=line.preparation,
                group_name=line.group,
            )
        )
    for position, step in enumerate(outcome.steps):
        session.add(
            RecipeStep(
                household_id=job.household_id,
                recipe_id=recipe.id,
                position=position,
                text=step["text"],
                group_name=step["group"],
                timers=step["timers"],
            )
        )
    return recipe.id


def _load(sessionmaker: Any, job_id: uuid.UUID) -> tuple[Any, ImportInput] | None:
    from thymble.models import ImportJob, ImportStatus

    with sessionmaker() as session:
        job = session.get(ImportJob, job_id)
        if job is None:
            return None
        job.status = ImportStatus.RUNNING
        job.started_at = datetime.now(UTC)
        session.commit()
        data = job.input or {}
        item = ImportInput(
            kind=str(job.source_kind),
            html=data.get("html"),
            final_url=job.final_url,
            url=job.requested_url,
            text=data.get("text"),
            photo=job.photo,
            photo_mime=job.photo_mime,
        )
        session.expunge(job)
        return job, item


def _finish(
    sessionmaker: Any, job_id: uuid.UUID, outcome: ImportOutcome | None, error: str | None
) -> None:
    from thymble.models import ImportJob, ImportStatus

    with sessionmaker() as session:
        job = session.get(ImportJob, job_id)
        if job is None:
            return
        job.finished_at = datetime.now(UTC)
        job.photo = None  # photos are only kept until they are read
        if outcome is not None:
            job.recipe_id = save_draft(session, job, outcome)
            job.result = outcome.to_result()
            job.needs_review = outcome.needs_review
            job.final_url = outcome.final_url
            job.status = ImportStatus.DONE
        else:
            job.status = ImportStatus.FAILED
            job.error = error
        session.commit()


async def import_recipe(ctx: dict[str, Any], job_id: str) -> str:
    """Run one import job. `ctx` may carry test doubles: sessionmaker, llm, fetch_transport."""
    from thymble.db.session import get_sessionmaker

    settings = ctx.get("settings") or get_settings()
    sessionmaker = ctx.get("sessionmaker") or get_sessionmaker()
    llm = ctx.get("llm") or get_llm_client()
    job_uuid = uuid.UUID(job_id)
    loaded = await asyncio.to_thread(_load, sessionmaker, job_uuid)
    if loaded is None:
        log.warning("import job %s not found", job_id)
        return "missing"
    job, item = loaded
    resolver = IngredientResolver(sessionmaker, llm, job.household_id, job.created_by)
    pipeline = ImportPipeline(
        settings, llm, job.household_id, resolver, fetch_transport=ctx.get("fetch_transport")
    )
    try:
        outcome = await pipeline.run(item)
    except ImportFailed as exc:
        await asyncio.to_thread(_finish, sessionmaker, job_uuid, None, str(exc))
        return "failed"
    except Exception:
        log.exception("import job %s crashed", job_id)
        await asyncio.to_thread(
            _finish, sessionmaker, job_uuid, None, "the import failed unexpectedly"
        )
        return "failed"
    await asyncio.to_thread(_finish, sessionmaker, job_uuid, outcome, None)
    return "done"


async def warm_up(ctx: dict[str, Any]) -> None:
    """Worker start-up: keep ingredient-parser-nlp loaded (~2 s once, ~2 ms per line after)."""
    await asyncio.to_thread(load_parser)
