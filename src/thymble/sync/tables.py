"""Tables devices can write today: recipes (with lines, steps, tags) and shopping lists.

Other features register their own tables the same way (see `thymble.sync.registry`).
"""

from __future__ import annotations

from typing import Any

from thymble.models import (
    Recipe,
    RecipeIngredient,
    RecipeStep,
    RecipeTag,
    ShoppingItem,
    ShoppingList,
    Tag,
)
from thymble.sync.registry import Op, SyncTable, WriteContext, register


def _recipe_author(
    ctx: WriteContext, row: Recipe, op: Op, inserting: bool, changes: dict[str, Any]
) -> None:
    if inserting:
        row.created_by = ctx.user.id


def _item_checker(
    ctx: WriteContext, row: ShoppingItem, op: Op, inserting: bool, changes: dict[str, Any]
) -> None:
    # Whoever uploads the check-off is who checked it; clients can't name someone else.
    if "checked_at" in changes:
        row.checked_by = ctx.user.id if row.checked_at is not None else None


register(SyncTable(Recipe, readonly=frozenset({"created_by"}), before_write=_recipe_author))
register(SyncTable(RecipeIngredient))
register(SyncTable(RecipeStep))
register(SyncTable(Tag))
register(SyncTable(RecipeTag))
register(SyncTable(ShoppingList))
register(SyncTable(ShoppingItem, readonly=frozenset({"checked_by"}), before_write=_item_checker))
