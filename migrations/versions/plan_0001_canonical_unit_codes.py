"""units: one set of unit codes for recipes and the shopping list

The importer and the shopping list used to write different codes for the same unit.
Both now use the table in `thymble.units`, whose codes are the importer's (and
ThymbleKit's): "fl oz", "pt", "qt", "piece", "package". This rewrites the shopping list's
old codes ("fl_oz", "pint", "quart", "ct", "pack") and spellings the old shopping
normalizer stored as-is ("tin", "packet", "boxe").

Columns: recipe_ingredient.unit, shopping_item.unit, shopping_item_source.unit,
item_store_pref.typical_unit. Data only, no schema change.

Downgrade puts the shopping list's old codes back ("fl_oz", "pint", "quart", "ct"); the
extra spellings stay mapped.

Revision ID: plan_0001
Revises: shop_0001
Create Date: 2026-10-29 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "plan_0001"
down_revision: str | None = "shop_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here on purpose: migrations must not change when thymble.units does.
OLD_TO_NEW = {
    "fl_oz": "fl oz",
    "fluid ounce": "fl oz",
    "pint": "pt",
    "quart": "qt",
    "ct": "piece",
    "pack": "package",
    "tin": "can",
    "packet": "package",
    "pkg": "package",
    "centiliter": "cl",
    "centilitre": "cl",
    "deciliter": "dl",
    "decilitre": "dl",
    "centimeter": "cm",
    "centimetre": "cm",
    "inche": "inch",
    "pinche": "pinch",
    "dashe": "dash",
    "bunche": "bunch",
    "boxe": "box",
}

COLUMNS = (
    ("recipe_ingredient", "unit"),
    ("shopping_item", "unit"),
    ("shopping_item_source", "unit"),
    ("item_store_pref", "typical_unit"),
)

# What each writer used before, for downgrade.
RECIPE_OLD: dict[str, str] = {}  # recipe lines already used these codes
SHOPPING_OLD = {"fl oz": "fl_oz", "pt": "pint", "qt": "quart", "piece": "ct"}


def _rewrite(table: str, column: str, mapping: dict[str, str]) -> None:
    for old, new in mapping.items():
        op.execute(
            sa.text(f'UPDATE "{table}" SET "{column}" = :new WHERE "{column}" = :old').bindparams(
                old=old, new=new
            )
        )


def upgrade() -> None:
    for table, column in COLUMNS:
        _rewrite(table, column, OLD_TO_NEW)


def downgrade() -> None:
    for table, column in COLUMNS:
        _rewrite(table, column, RECIPE_OLD if table == "recipe_ingredient" else SHOPPING_OLD)
