"""Tables devices can write through the PowerSync upload endpoint.

Recipes (with lines, steps, tags; imported drafts are finalized by clearing `is_draft`),
the meal plan, the shopping list with item provenance, trips, stores, store groups and
per-item store preferences. Every write is household-checked by the upload applier.

Not writable from devices: server-only tables (auth_*, household_invite, import_job,
llm_usage, llm_cache, connector_*, learning_event) and the catalog (ingredient,
ingredient_alias), which the import pipeline and the api maintain. Columns the server
owns are read-only below; the matching REST endpoints change them with the domain logic
that goes with them (trip export/confirm, stop-gap links, connector routing).

Other features register their own tables the same way (see `thymble.sync.registry`).
"""

from __future__ import annotations

import uuid
from typing import Any

from thymble.models import (
    ItemSourceType,
    ItemStorePref,
    MealPlanEntry,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    RecipeTag,
    ShoppingItem,
    ShoppingItemSource,
    ShoppingList,
    Store,
    StoreGroup,
    StoreGroupMember,
    Tag,
    Trip,
)
from thymble.shopping import mealplan
from thymble.shopping.service import ShoppingService
from thymble.sync.registry import Op, SyncTable, WriteContext, WriteRejected, register


def _recipe_author(
    ctx: WriteContext, row: Recipe, op: Op, inserting: bool, changes: dict[str, Any]
) -> None:
    if inserting:
        row.created_by = ctx.user.id


def _shopping(ctx: WriteContext, household_id: uuid.UUID) -> ShoppingService:
    return ShoppingService(ctx.db, household_id, ctx.user.id)


def _item_rules(
    ctx: WriteContext, row: ShoppingItem, op: Op, inserting: bool, changes: dict[str, Any]
) -> None:
    # Whoever uploads the check-off is who checked it; clients can't name someone else.
    if "checked_at" in changes:
        row.checked_by = ctx.user.id if row.checked_at is not None else None
    # Urgency (and its stop-gap), re-assignment and check-offs: the REST rules.
    _shopping(ctx, row.household_id).apply_client_change(row, changes)


def _plan_entry_delete(ctx: WriteContext, row: MealPlanEntry) -> None:
    mealplan.delete_entry(_shopping(ctx, row.household_id), row)


def _item_delete(ctx: WriteContext, row: ShoppingItem) -> None:
    # A deleted bulk item takes its stop-gaps with it, as on REST.
    _shopping(ctx, row.household_id).delete_item(row)


def _trip_shopper(
    ctx: WriteContext, row: Trip, op: Op, inserting: bool, changes: dict[str, Any]
) -> None:
    # A trip started on a device belongs to whoever started it.
    if inserting:
        row.user_id = ctx.user.id


def _source_kind(
    ctx: WriteContext, row: ShoppingItemSource, op: Op, inserting: bool, changes: dict[str, Any]
) -> None:
    # Connector provenance is written by the connector supervisor only.
    if row.source_type == ItemSourceType.CONNECTOR:
        raise WriteRejected("connector sources are written by the server")


# Recipes (an import lands as a draft; the app clears is_draft once the user has reviewed it).
register(SyncTable(Recipe, readonly=frozenset({"created_by"}), before_write=_recipe_author))
register(SyncTable(RecipeIngredient))
register(SyncTable(RecipeStep))
register(SyncTable(Tag))
register(SyncTable(RecipeTag))
# Removing a plan entry takes its share off the shopping lists, as DELETE /v1/meal-plan does.
register(SyncTable(MealPlanEntry, on_delete=_plan_entry_delete))

# Shopping. Order export/confirmation (trip_id, status on_order) and stop-gap links
# (bridge_of) go through the REST endpoints, which apply the rules in thymble.shopping.
# Urgency, store assignment, check-offs and deletes sync, and run through the same
# ShoppingService methods as REST: a stop-gap appears or goes away the same either way.
register(SyncTable(ShoppingList))
register(
    SyncTable(
        ShoppingItem,
        readonly=frozenset({"checked_by", "trip_id", "bridge_of"}),
        before_write=_item_rules,
        on_delete=_item_delete,
    )
)
register(
    SyncTable(
        ShoppingItemSource,
        readonly=frozenset({"connector_account_id"}),
        before_write=_source_kind,
    )
)
register(
    SyncTable(
        Trip,
        columns=frozenset({"store_id", "kind", "expected_pickup_at"}),
        before_write=_trip_shopper,
    )
)

# Stores and preferences. Linking a store to an external list needs a connector account,
# which is server-only, so that stays on the REST endpoint.
register(
    SyncTable(Store, readonly=frozenset({"external_list_connector_account_id", "external_list_id"}))
)
register(SyncTable(StoreGroup))
register(SyncTable(StoreGroupMember))
register(SyncTable(ItemStorePref))
