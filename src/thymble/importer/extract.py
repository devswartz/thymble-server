"""Structured recipe extraction (architecture §5.2).

Three readers, merged per field: our own schema.org JSON-LD reader (keeps raw values:
`recipeYield`, ISO-8601 durations, `HowToSection` names), the `recipe-scrapers` generic
schema.org parse, and the `recipe-scrapers` site scraper for the page's host. A field comes
from the first reader that has it; ingredient groups come from whichever reader names them.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from thymble.importer import rules, textnorm

log = logging.getLogger("thymble.importer")

# recipe-scrapers dispatches to the site class for supported hosts even with
# supported_only=False, so a true generic parse needs a host with no site scraper.
GENERIC_HOST = "https://generic-schema-org.invalid/recipe"


@dataclass
class RecipeDraft:
    title: str | None = None
    description: str | None = None
    image_url: str | None = None
    source_url: str | None = None
    language: str | None = None
    yield_raw: Any = None
    prep_time_raw: Any = None
    cook_time_raw: Any = None
    total_time_raw: Any = None
    prep_time_minutes: int | None = None
    cook_time_minutes: int | None = None
    total_time_minutes: int | None = None
    # (text, group name) in page order
    ingredients: list[tuple[str, str | None]] = field(default_factory=list)
    steps: list[tuple[str, str | None]] = field(default_factory=list)
    extracted_by: list[str] = field(default_factory=list)
    field_sources: dict[str, str] = field(default_factory=dict)
    # Fields where the readers disagreed (import review, D-034): "title", "yield", "times"
    # (list of time fields), "ingredients" / "steps" (comparable texts of the chosen lines
    # another reader does not have).
    disagreements: dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.title and self.ingredients and self.steps)


# ------------------------------------------------------------------------------- JSON-LD


def _types(node: dict[str, Any]) -> list[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return [str(v).split("/")[-1] for v in values if v]


def find_jsonld_recipe(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack: list[Any] = [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "Recipe" in _types(node):
                    return node
                stack.extend(node.values())
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = next((v for v in value if v), None)
        return _text(value)
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("name"))
    text = BeautifulSoup(str(value), "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _image(value: Any) -> str | None:
    if isinstance(value, list):
        return next((img for img in (_image(v) for v in value) if img), None)
    if isinstance(value, dict):
        return _image(value.get("url") or value.get("contentUrl"))
    return value if isinstance(value, str) and value.strip() else None


def _instructions(value: Any, section: str | None = None) -> list[tuple[str, str | None]]:
    if value is None:
        return []
    if isinstance(value, str):
        text = _text(value) or ""
        parts = [p for p in re.split(r"\n+|(?<=[.!?])\s{2,}", text) if p.strip()]
        return [(p.strip(), section) for p in parts]
    if isinstance(value, list):
        return [step for item in value for step in _instructions(item, section)]
    if isinstance(value, dict):
        types = _types(value)
        if "HowToSection" in types:
            name = _text(value.get("name")) or section
            return _instructions(value.get("itemListElement"), name)
        if "ItemList" in types:
            return _instructions(value.get("itemListElement"), section)
        text = _text(value.get("text") or value.get("name"))
        return [(text, section)] if text else []
    return []


def read_jsonld(node: dict[str, Any]) -> RecipeDraft:
    ingredients = node.get("recipeIngredient") or node.get("ingredients") or []
    if isinstance(ingredients, str):
        ingredients = [ingredients]
    return RecipeDraft(
        title=_text(node.get("name")),
        description=_text(node.get("description")),
        image_url=_image(node.get("image")),
        language=rules.normalize_lang(node.get("inLanguage")),
        yield_raw=node.get("recipeYield"),
        prep_time_raw=node.get("prepTime"),
        cook_time_raw=node.get("cookTime"),
        total_time_raw=node.get("totalTime"),
        ingredients=[(t, None) for t in (_text(i) for i in ingredients) if t],
        steps=_instructions(node.get("recipeInstructions")),
        extracted_by=["jsonld"],
    )


# ------------------------------------------------------------------------------- recipe-scrapers


def _call(scraper: Any, method: str) -> Any:
    try:
        return getattr(scraper, method)()
    except Exception:  # each field can fail independently in recipe-scrapers
        return None


def read_scraper(html: str, url: str, generic: bool) -> RecipeDraft | None:
    from recipe_scrapers import scrape_html

    try:
        if generic:
            scraper = scrape_html(html, org_url=GENERIC_HOST, supported_only=False)
        else:
            scraper = scrape_html(html, org_url=url)
    except Exception as exc:
        log.debug("recipe-scrapers (%s) could not read %s: %s", generic, url, exc)
        return None
    draft = RecipeDraft(extracted_by=["schema.org" if generic else "site-scraper"])
    draft.title = _text(_call(scraper, "title"))
    draft.description = _text(_call(scraper, "description"))
    draft.image_url = _image(_call(scraper, "image"))
    draft.language = rules.normalize_lang(_call(scraper, "language"))
    draft.yield_raw = _call(scraper, "yields")
    for attr in ("prep_time", "cook_time", "total_time"):
        minutes = _call(scraper, attr)
        if isinstance(minutes, int | float) and minutes > 0:
            setattr(draft, f"{attr}_minutes", int(minutes))
    groups = _call(scraper, "ingredient_groups") or []
    named = [g for g in groups if getattr(g, "purpose", None)]
    if named:
        draft.ingredients = [(i, g.purpose) for g in groups for i in g.ingredients]
    else:
        draft.ingredients = [(i, None) for i in _call(scraper, "ingredients") or [] if i]
    draft.steps = [(s, None) for s in _call(scraper, "instructions_list") or [] if s]
    return draft


# ------------------------------------------------------------------------------- merge

_FIELDS = (
    "title",
    "description",
    "image_url",
    "language",
    "yield_raw",
    "prep_time_raw",
    "cook_time_raw",
    "total_time_raw",
    "prep_time_minutes",
    "cook_time_minutes",
    "total_time_minutes",
    "ingredients",
    "steps",
)


def merge(drafts: list[RecipeDraft]) -> RecipeDraft:
    out = RecipeDraft()
    for name in _FIELDS:
        for draft in drafts:
            value = getattr(draft, name)
            if value not in (None, "", []):
                setattr(out, name, value)
                out.field_sources[name] = draft.extracted_by[0]
                break
    # Named ingredient groups are often lost in schema.org data but kept by the site scraper.
    if not any(group for _, group in out.ingredients):
        for draft in drafts:
            if any(group for _, group in draft.ingredients) and len(draft.ingredients) >= len(
                out.ingredients
            ):
                out.ingredients = draft.ingredients
                out.field_sources["ingredients"] = draft.extracted_by[0]
                break
    out.extracted_by = sorted(set(out.field_sources.values()))
    out.disagreements = disagreements(drafts, out)
    return out


def disagreements(drafts: list[RecipeDraft], chosen: RecipeDraft) -> dict[str, Any]:
    """Where the readers (schema.org JSON-LD, generic and site scraper) don't agree on the
    value the merge chose. Compared as normalized text, numbers and minutes."""
    found: dict[str, Any] = {}
    if len(drafts) < 2:
        return found
    titles = {textnorm.comparable(rules.clean_title(d.title)) for d in drafts if d.title}
    if len(titles) > 1:
        found["title"] = sorted(titles)
    yields = {
        rules.parse_yield(d.yield_raw)["quantity"]
        for d in drafts
        if d.yield_raw not in (None, "", [])
    } - {None}
    if len(yields) > 1:
        found["yield"] = sorted(yields)  # type: ignore[type-var]
    for attr in ("prep_time", "cook_time", "total_time"):
        values = {
            getattr(d, f"{attr}_minutes") or rules.parse_duration(getattr(d, f"{attr}_raw"))
            for d in drafts
        } - {None}
        if len(values) > 1:
            found.setdefault("times", []).append(attr)
    for name in ("ingredients", "steps"):
        mine = [textnorm.comparable(text) for text, _ in getattr(chosen, name)]
        odd: set[str] = set()
        for draft in drafts:
            theirs = [textnorm.comparable(text) for text, _ in getattr(draft, name)]
            if not theirs:
                continue
            if name == "ingredients":
                odd |= {k for k in mine if k not in set(theirs)}
            else:
                joined = " ".join(theirs)
                odd |= {k for k in mine if k not in joined}
        if odd:
            found[name] = sorted(odd)
    return found


def page_title_h1(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True) if h1 else None


def html_language(html: str) -> str | None:
    m = re.search(r"<html[^>]*\blang=[\"']?([a-zA-Z]{2})", html[:5000], flags=re.I)
    return m.group(1).lower() if m else None


def extract_from_html(html: str, url: str | None) -> RecipeDraft | None:
    """Structured data from a page, or None when the page has no recipe markup."""
    drafts: list[RecipeDraft] = []
    node = find_jsonld_recipe(html)
    if node is not None:
        drafts.append(read_jsonld(node))
    generic = read_scraper(html, url or GENERIC_HOST, generic=True)
    if generic is not None:
        drafts.append(generic)
    if url:
        site = read_scraper(html, url, generic=False)
        if site is not None:
            drafts.append(site)
    drafts = [d for d in drafts if d.title or d.ingredients]
    if not drafts:
        return None
    draft = merge(drafts)
    draft.title = rules.clean_title(draft.title, page_title_h1(html))
    draft.language = draft.language or html_language(html)
    draft.source_url = url
    return draft


def html_to_text(html: str, limit: int = 40_000) -> str:
    """Visible page text for the LLM fallback (T1): scripts, styles and navigation removed."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    text = root.get_text("\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:limit]


# ------------------------------------------------------------------------------- plain text

_HEAD_INGREDIENTS = re.compile(r"^\s*(ingredients?|zutaten|ingrédients|ingredienti)\s*:?\s*$", re.I)
_HEAD_STEPS = re.compile(
    r"^\s*(instructions?|directions?|method|steps?|preparation|zubereitung|préparation"
    r"|procedimento)\s*:?\s*$",
    re.I,
)


def extract_from_text(text: str) -> RecipeDraft | None:
    """Pasted or transcribed text with "Ingredients" / "Instructions" headings. Returns None
    when the headings are missing (the LLM structures it instead)."""
    lines = [line.strip() for line in text.splitlines()]
    ing_at = next((i for i, line in enumerate(lines) if _HEAD_INGREDIENTS.match(line)), None)
    step_at = next((i for i, line in enumerate(lines) if _HEAD_STEPS.match(line)), None)
    if ing_at is None or step_at is None or step_at <= ing_at:
        return None
    title = next((line for line in lines[:ing_at] if line), None)
    description = " ".join(line for line in lines[1:ing_at] if line) or None if title else None
    ingredients: list[tuple[str, str | None]] = []
    group: str | None = None
    for line in lines[ing_at + 1 : step_at]:
        if not line:
            continue
        line = re.sub(r"^[-*•]\s*", "", line)
        if line.endswith(":") and len(line.split()) <= 6:
            group = line.rstrip(":").strip()
            continue
        ingredients.append((line, group))
    steps: list[tuple[str, str | None]] = []
    for line in lines[step_at + 1 :]:
        line = re.sub(r"^(?:\d+[.)]|step \d+[:.]?|[-*•])\s*", "", line, flags=re.I).strip()
        if line:
            steps.append((line, None))
    if not ingredients or not steps:
        return None
    return RecipeDraft(
        title=title,
        description=description,
        ingredients=ingredients,
        steps=steps,
        language=rules.detect_language(text),
        extracted_by=["text-rules"],
    )
