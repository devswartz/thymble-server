"""Confidence thresholds and tables for import review (D-034, D-035; architecture §5.8).

Everything that decides *how sure* an imported item is lives here, so it can be calibrated
against the golden set ("high" must be right >= 98% of the time) without touching the code
that gathers the evidence (`thymble.importer.confidence`, `thymble.importer.readings`).

Confidence comes from evidence only. Each reason code has a fixed level; an item's level is
the most severe of its reasons, and an item with no reasons is "high". Model self-ratings
are never an input here.
"""

from __future__ import annotations

import enum


class Level(enum.StrEnum):
    HIGH = "high"
    CHECK = "check"
    LOW = "low"


SEVERITY = {Level.HIGH: 0, Level.CHECK: 1, Level.LOW: 2}

# ------------------------------------------------------------------------- parser and OCR

# ingredient-parser-nlp: lowest field confidence below this -> "check"; below LOW -> "low".
PARSER_CHECK_BELOW = 0.90
PARSER_LOW_BELOW = 0.50
# The device's own OCR confidence for a line (Apple Vision, 0..1) below this -> "check",
# even when both readings agree.
DEVICE_OCR_CHECK_BELOW = 0.50

# Line alignment of the two photo readings: lines pair up when their normalized text is at
# least this similar (difflib ratio); otherwise a line counts as seen by one reader only.
READING_PAIR_MIN_SIMILARITY = 0.60
# Numbers in the two readings are equal when they differ by less than this (0.33 vs 1/3).
NUMBER_TOLERANCE = 0.01
# Mapping a structured item (ingredient, step, title) back to transcription lines.
ITEM_MATCH_MIN_SIMILARITY = 0.80

# A step with at most this many words and no sentence punctuation may be a heading.
STEP_HEADING_MAX_WORDS = 5

# Most lines a photo reading may have; longer inputs are cut (keeps alignment O(n*m) small).
MAX_READING_LINES = 400

# ------------------------------------------------------------------------- reason codes

# code -> (level, plain-language text). Texts may use {placeholders} filled by the caller.
REASONS: dict[str, tuple[Level, str]] = {
    # ingredient parsing
    "parser_unsure": (Level.CHECK, "the line parser wasn't sure how to split this line"),
    "parser_very_unsure": (Level.LOW, "the line parser could hardly make sense of this line"),
    "no_name": (Level.LOW, "couldn't find the ingredient in this line"),
    "llm_parsed": (Level.CHECK, "the AI had to parse this line"),
    "llm_failed": (Level.CHECK, "the AI couldn't double-check this line ({detail})"),
    "ditto_copied": (Level.CHECK, "the amount or item was copied from the line above"),
    "or_split": (Level.CHECK, 'split into an ingredient and substitutes at "or"'),
    "non_english": (Level.CHECK, "the recipe isn't in English, so parsing is less reliable"),
    "implausible_amount": (Level.LOW, "{detail} looks unlikely; check the amount and unit"),
    "unknown_unit": (Level.CHECK, 'didn\'t recognise the unit "{detail}"'),
    "catalog_ai_match": (Level.CHECK, 'not in the catalog yet; the AI matched it to "{detail}"'),
    "catalog_new": (Level.CHECK, "not in the ingredient catalog; added as written"),
    # page extraction
    "sources_disagree": (Level.CHECK, "the page's recipe data and the site reader disagree"),
    "ai_extracted": (Level.CHECK, "taken from the page by the AI, not from recipe data"),
    "ai_structured": (Level.CHECK, "put together by the AI"),
    "redirected": (Level.CHECK, "the link redirected to another page"),
    "redirect_title_mismatch": (
        Level.LOW,
        "the link redirected to a page whose title doesn't match the link",
    ),
    "partial_data": (Level.CHECK, "the page's recipe data looks incomplete ({detail})"),
    "missing_title": (Level.LOW, "no title found"),
    "missing_steps": (Level.LOW, "no steps found"),
    "missing_yield": (Level.CHECK, "no servings or yield found"),
    "yield_unparsed": (Level.CHECK, 'couldn\'t read a number from the yield "{detail}"'),
    "times_inconsistent": (Level.CHECK, "the prep and cook times don't add up to the total"),
    "time_implausible": (Level.CHECK, "a time of {detail} looks unlikely"),
    "step_maybe_heading": (Level.CHECK, "this step might be a section heading"),
    "heading_from_steps": (Level.CHECK, "section headings were found among the steps"),
    # photos
    "photo_unreadable": (Level.LOW, "a word couldn't be read in the photo"),
    "photo_cut_off": (Level.LOW, "text runs off the edge of the photo"),
    "photo_single_reading": (
        Level.CHECK,
        "read from the photo by the AI only; there was no second reading to compare",
    ),
    "readings_number": (Level.LOW, "the two readings of the photo differ in a number: {detail}"),
    "readings_unit": (Level.LOW, "the two readings of the photo differ in a unit: {detail}"),
    "readings_text": (Level.CHECK, "the two readings of the photo differ: {detail}"),
    "readings_missing_number": (
        Level.LOW,
        "only one reading of the photo has this line, and it has a number in it",
    ),
    "readings_missing": (Level.CHECK, "only one reading of the photo has this line"),
    "device_ocr_unsure": (Level.CHECK, "the phone's reading of this line was unsure"),
    "photo_unmatched": (
        Level.CHECK,
        "couldn't find this in the photo's text, so it couldn't be compared",
    ),
    "photo_lines_dropped": (
        Level.CHECK,
        "{detail} line(s) the phone read aren't in the recipe; check nothing is missing",
    ),
}

# ------------------------------------------------------------------------- plausibility

# Largest quantity that is plausible for one recipe line, by canonical unit code (see
# thymble.importer.rules.norm_unit). Units not listed are not checked. None = no unit.
MAX_QUANTITY_BY_UNIT: dict[str | None, float] = {
    None: 60,
    "cup": 16,
    "tbsp": 32,
    "tsp": 24,
    "fl oz": 128,
    "ml": 5000,
    "cl": 500,
    "dl": 50,
    "l": 10,
    "pt": 8,
    "qt": 8,
    "gal": 3,
    "g": 10000,
    "kg": 10,
    "mg": 5000,
    "oz": 128,
    "lb": 20,
    "pinch": 6,
    "dash": 10,
    "drop": 50,
    "clove": 40,
    "stick": 8,
    "can": 12,
    "package": 12,
    "jar": 8,
    "bottle": 6,
    "bag": 6,
    "slice": 40,
    "sprig": 30,
    "sheet": 30,
    "bunch": 8,
    "head": 6,
    "stalk": 20,
    "handful": 8,
    "piece": 50,
    "inch": 30,
    "cm": 60,
}

# Unit -> teaspoons (volume) or grams (weight), for per-ingredient limits.
TSP_PER_UNIT: dict[str, float] = {
    "tsp": 1.0,
    "tbsp": 3.0,
    "cup": 48.0,
    "fl oz": 6.0,
    "ml": 0.2029,
    "cl": 2.029,
    "dl": 20.29,
    "l": 202.9,
    "pt": 96.0,
    "qt": 192.0,
    "gal": 768.0,
    "pinch": 1 / 16,
    "dash": 1 / 8,
    "drop": 1 / 96,
}
GRAMS_PER_UNIT: dict[str, float] = {
    "g": 1.0,
    "kg": 1000.0,
    "mg": 0.001,
    "oz": 28.35,
    "lb": 453.6,
}

# (pattern searched as whole words in the ingredient name, max teaspoons, max grams, label).
# Checked in order; the first match wins. Generous on purpose: brines and spice rubs exist.
INGREDIENT_LIMITS: list[tuple[str, float, float, str]] = [
    (r"cayenne|chili flakes|red pepper flakes", 6, 20, "hot spice"),
    (r"baking soda|bicarbonate of soda|baking powder|cream of tartar", 12, 60, "leavening"),
    (r"yeast", 12, 60, "yeast"),
    (r"(?:vanilla|almond|lemon|peppermint) extract", 12, 60, "extract"),
    (r"salt", 48, 300, "salt"),
    (
        r"(?:ground )?(?:cinnamon|cumin|paprika|nutmeg|turmeric|allspice)|ground cloves"
        r"|ground ginger|chili powder|curry powder|(?:black|white|ground) pepper|^pepper$",
        24,
        150,
        "spice",
    ),
]

# Longest plausible single time on a recipe, in minutes (2 days: brines, doughs).
MAX_TIME_MINUTES = 2 * 24 * 60
