"""The import pipeline (architecture §5): input -> structured draft -> parsed, normalized lines.

Stages are separate functions so each can be retried and improved on its own:
  acquire   device HTML | polite server fetch | pasted text | photo (two LLM passes, D-019)
  extract   schema.org + recipe-scrapers merged per field; LLM (T1/T2) only when that fails
  lines     rules + ingredient-parser-nlp; LLM (T3) for low-confidence, "or"/"sub", non-English
  normalize compound dictionary + catalog aliases; LLM (T4) for unseen names
  steps     section-heading detection + timers
  review    confidence (high / check / low) with reasons for every line, step and section,
            from evidence only (D-034); photos are checked against the device's own OCR
            reading line by line (D-035)
The outcome is saved as a draft recipe for the user to review (§5.8); anything the pipeline
could not settle is listed in `review_reasons` rather than failing the import.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from thymble.config import Settings
from thymble.importer import confidence, llm_tasks, readings, rules
from thymble.importer.extract import (
    RecipeDraft,
    extract_from_html,
    extract_from_text,
    html_to_text,
)
from thymble.importer.fetch import FetchError, fetch_page
from thymble.importer.ingredients import ParsedLine, parse_lines
from thymble.importer.normalize import apply_dictionary
from thymble.llm import ImageInput, LLMClient

log = logging.getLogger("thymble.importer")

PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class ImportFailed(Exception):
    """No recipe could be produced; the message is shown to the user."""


class Resolver(Protocol):
    async def resolve(self, lines: list[ParsedLine]) -> None: ...


@dataclass
class ImportInput:
    kind: str  # html | url | text | photo
    html: str | None = None
    final_url: str | None = None
    url: str | None = None
    text: str | None = None
    photo: bytes | None = None
    photo_mime: str | None = None
    # Photos: the device's own OCR of the same photo (D-035) and, optionally, its
    # confidence per line of that text (0..1, one per line including blank ones).
    device_ocr_text: str | None = None
    device_ocr_confidences: list[float] | None = None


@dataclass
class ImportOutcome:
    draft: RecipeDraft
    lines: list[ParsedLine]
    steps: list[dict[str, Any]]
    yield_info: dict[str, Any]
    language: str
    final_url: str | None
    review_reasons: list[str] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    transcription: list[str] | None = None
    readings: readings.Readings | None = None
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_state: str = "needs_review"
    confidence_counts: dict[str, int] = field(default_factory=dict)

    @property
    def needs_review(self) -> bool:
        return self.review_state == "needs_review"

    def to_result(self) -> dict[str, Any]:
        d = self.draft
        return {
            "title": d.title,
            "description": d.description,
            "image_url": d.image_url,
            "final_url": self.final_url,
            "language": self.language,
            "yield": self.yield_info,
            "raw": {
                "recipeYield": d.yield_raw,
                "prepTime": d.prep_time_raw,
                "cookTime": d.cook_time_raw,
                "totalTime": d.total_time_raw,
            },
            "times": {
                "prep_minutes": d.prep_time_minutes,
                "cook_minutes": d.cook_time_minutes,
                "total_minutes": d.total_time_minutes,
            },
            "extracted_by": d.extracted_by,
            "field_sources": d.field_sources,
            "ingredients": [line.to_dict() for line in self.lines],
            "steps": self.steps,
            "transcription": self.transcription,
            "readings": self.readings.to_dict() if self.readings else None,
            "stages": self.stages,
            "needs_review": self.needs_review,
            "review_reasons": self.review_reasons,
            "review": {
                "state": self.review_state,
                "counts": self.confidence_counts,
                "sections": self.sections,
            },
        }


class ImportPipeline:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        household_id: uuid.UUID | None = None,
        resolver: Resolver | None = None,
        fetch_transport: Any = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.household_id = household_id
        self.resolver = resolver
        self._fetch_transport = fetch_transport

    async def run(self, item: ImportInput) -> ImportOutcome:
        reasons: list[str] = []
        stages: list[str] = []
        transcription: list[str] | None = None
        resolved: list[str] | None = None
        self._notes: list[tuple[str, str | None]] = []
        final_url = item.final_url
        if item.kind == "photo":
            draft, transcription, resolved = await self._from_photo(item, reasons, stages)
        elif item.kind == "text":
            draft = await self._from_text(item.text or "", reasons, stages)
        else:
            html = item.html
            if item.kind == "url":
                page = await self._fetch(item.url or "")
                html, final_url = page.html, page.final_url
                stages.append("fetch")
                if confidence.different_page(item.url, final_url):
                    reasons.append("the link redirected to a different page; check the title")
            draft = await self._from_html(html or "", final_url, reasons, stages)
        draft.source_url = final_url

        language = draft.language or rules.detect_language(
            " ".join(t for t, _ in draft.ingredients + draft.steps)
        )
        lines = parse_lines(draft.ingredients, language)
        stages.append("parse")
        await self._llm_lines(lines, language, stages)
        lines = _expand_additional(lines)
        for line in lines:
            apply_dictionary(line)
        if self.resolver is not None:
            await self.resolver.resolve(lines)
            stages.append("normalize")

        split = rules.split_steps(draft.steps)
        steps = [
            {"text": text, "group": group, "timers": rules.detect_timers(text) or None}
            for text, group in split
        ]
        # Groups that came from headings the page returned as steps (a rule that can misfire).
        heading_groups = {g for _, g in split if g} - {g for _, g in draft.steps if g}
        yield_info = rules.parse_yield(draft.yield_raw)
        for attr in ("prep_time", "cook_time", "total_time"):
            if getattr(draft, f"{attr}_minutes") is None:
                setattr(
                    draft, f"{attr}_minutes", rules.parse_duration(getattr(draft, f"{attr}_raw"))
                )
        if not draft.title:
            reasons.append("no title found")
        if not steps:
            reasons.append("no steps found")

        second: readings.Readings | None = None
        if item.kind == "photo" and transcription is not None and item.device_ocr_text:
            device, scores = readings.split_device_text(
                item.device_ocr_text, item.device_ocr_confidences
            )
            if device:
                second = readings.align(transcription, device, scores)
                stages.append("compare-readings")
        report = confidence.assess_import(
            kind=item.kind,
            draft=draft,
            lines=lines,
            steps=steps,
            yield_info=yield_info,
            requested_url=item.url,
            final_url=final_url,
            heading_groups=heading_groups,
            transcription=transcription,
            resolved_transcription=resolved,
            readings=second,
            notes=self._notes,
        )
        stages.append("confidence")
        return ImportOutcome(
            draft=draft,
            lines=lines,
            steps=steps,
            yield_info=yield_info,
            language=language,
            final_url=final_url,
            review_reasons=reasons,
            stages=stages,
            transcription=transcription,
            readings=second,
            sections=report.sections,
            review_state=report.review_state,
            confidence_counts=report.counts,
        )

    # ------------------------------------------------------------------ acquire + extract

    async def _fetch(self, url: str):
        if not self.settings.import_server_fetch_enabled:
            raise ImportFailed("server-side fetching is turned off; share the page from the app")
        try:
            return await fetch_page(
                url,
                public_url=self.settings.thymble_public_url,
                timeout=self.settings.import_fetch_timeout_seconds,
                max_bytes=self.settings.import_max_html_bytes,
                transport=self._fetch_transport,
                check_address=self._fetch_transport is None,
            )
        except FetchError as exc:
            raise ImportFailed(str(exc)) from exc

    async def _from_html(
        self, html: str, url: str | None, reasons: list[str], stages: list[str]
    ) -> RecipeDraft:
        draft = extract_from_html(html, url)
        stages.append("extract")
        if draft is not None and draft.complete:
            return draft
        # No recipe markup, or clearly partial: T1 on the visible page text.
        result = await llm_tasks.extract_page_text(self.llm, html_to_text(html), self.household_id)
        stages.append("llm:T1")
        llm_draft = llm_tasks.draft_from_json(result.data, "llm:T1") if result.ok else None
        if llm_draft is None:
            if draft is not None and draft.ingredients:
                reasons.append(f"the page's recipe data is incomplete ({_reason(result)})")
                self._notes.append(("partial_data", _reason(result)))
                return draft
            raise ImportFailed(f"no recipe found on this page ({_reason(result)})")
        reasons.append("extracted by AI from the page text; please check it")
        if draft is None:
            return llm_draft
        # Keep what the structured data had; let the LLM fill the gaps.
        for name in ("title", "description", "ingredients", "steps", "yield_raw"):
            if not getattr(draft, name):
                setattr(draft, name, getattr(llm_draft, name))
                draft.field_sources[name] = "llm:T1"
        draft.extracted_by = sorted({*draft.extracted_by, "llm:T1"})
        return draft

    async def _from_text(self, text: str, reasons: list[str], stages: list[str]) -> RecipeDraft:
        if not text.strip():
            raise ImportFailed("the text is empty")
        draft = extract_from_text(text)
        stages.append("extract")
        if draft is not None:
            return draft
        result = await llm_tasks.structure_text(self.llm, text, self.household_id)
        stages.append("llm:T2")
        draft = llm_tasks.draft_from_json(result.data, "llm:T2") if result.ok else None
        if draft is None:
            raise ImportFailed(f"could not find a recipe in the text ({_reason(result)})")
        reasons.append("structured by AI; please check it")
        return draft

    async def _from_photo(
        self, item: ImportInput, reasons: list[str], stages: list[str]
    ) -> tuple[RecipeDraft, list[str], list[str]]:
        if not item.photo or item.photo_mime not in PHOTO_TYPES:
            raise ImportFailed("the photo must be a JPEG, PNG, WebP or GIF image")
        image = ImageInput(item.photo, item.photo_mime)
        # Pass (a): literal transcription only.
        result = await llm_tasks.transcribe_photo(self.llm, image, self.household_id)
        stages.append("llm:T2:transcribe")
        if not result.ok or result.data is None:
            raise ImportFailed(f"could not read the photo ({_reason(result)})")
        lines = [str(line) for line in result.data["lines"]]
        # Between the passes: deterministic ditto resolution ("do.", "same as above").
        resolved = rules.resolve_ditto_lines(lines)
        stages.append("ditto")
        text = "\n".join(resolved)
        # Pass (b): text-only structuring (rules first when the text has clear headings).
        draft = extract_from_text(text)
        if draft is None:
            structured = await llm_tasks.structure_text(self.llm, text, self.household_id)
            stages.append("llm:T2:structure")
            draft = llm_tasks.draft_from_json(structured.data, "llm:T2") if structured.ok else None
            if draft is None:
                raise ImportFailed(f"could not find a recipe in the photo ({_reason(structured)})")
        reasons.append("read from a photo; please check it against the original")
        return draft, lines, resolved

    # ------------------------------------------------------------------ lines

    async def _llm_lines(self, lines: list[ParsedLine], language: str, stages: list[str]) -> None:
        routed = [line for line in lines if line.route_to_llm and not line.ditto]
        if not routed:
            return
        stages.append(f"llm:T3x{len(routed)}")
        for line in routed:
            result = await llm_tasks.parse_line_llm(self.llm, line, language, self.household_id)
            if result.ok and result.data is not None:
                llm_tasks.apply_line_parse(line, result.data)
            else:
                line.review_reasons.append(
                    f"check this line ({', '.join(line.flags)}; {_reason(result)})"
                )


def _reason(result: Any) -> str:
    return getattr(result, "reason", None) or "the AI could not structure it"


def _expand_additional(lines: list[ParsedLine]) -> list[ParsedLine]:
    """ "salt and pepper" -> two lines (same raw_text), so both reach the shopping list."""
    out: list[ParsedLine] = []
    for line in lines:
        out.append(line)
        each = "each" in (line.note or "")
        for name in line.additional:
            out.append(
                ParsedLine(
                    raw_text=line.raw_text,
                    text=line.text,
                    quantity=line.quantity if each else None,
                    quantity_max=line.quantity_max if each else None,
                    unit=line.unit if each else None,
                    name=name,
                    note=line.note,
                    optional=line.optional,
                    parser_confidence=line.parser_confidence,
                    group=line.group,
                    source=line.source,
                )
            )
        line.additional = []
    return out
