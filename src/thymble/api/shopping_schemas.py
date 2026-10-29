"""Request/response models for the shopping, store, trip and connector endpoints."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from thymble.models import ItemStatus, TripKind, TripStatus, Urgency


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- lists / items
class ListIn(BaseModel):
    name: str = Field(min_length=1)
    is_active: bool = True


class ListPatch(BaseModel):
    name: str | None = None
    is_active: bool | None = None


class ListOut(_Out):
    id: uuid.UUID
    name: str
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class ItemOut(_Out):
    id: uuid.UUID
    list_id: uuid.UUID
    ingredient_id: uuid.UUID | None
    display_text: str
    quantity: Decimal | None
    unit: str | None
    status: ItemStatus
    urgency: Urgency
    store_id: uuid.UUID | None
    store_group_id: uuid.UUID | None
    bridge_of: uuid.UUID | None
    trip_id: uuid.UUID | None
    aisle: str | None
    checked_at: dt.datetime | None
    checked_by: uuid.UUID | None
    created_at: dt.datetime
    updated_at: dt.datetime


class ItemIn(BaseModel):
    """Either `text` (free text, parsed: "urgent 2 lb chicken") or `name` (+ quantity)."""

    text: str | None = None
    name: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    store_id: uuid.UUID | None = None
    store_group_id: uuid.UUID | None = None
    remember_store: bool = True
    urgent: bool = False


class ItemPatch(BaseModel):
    display_text: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    clear_quantity: bool = False
    aisle: str | None = None
    urgent: bool | None = None
    # Assignment: send `assign: true` with store_id / store_group_id (both null = none).
    assign: bool = False
    store_id: uuid.UUID | None = None
    store_group_id: uuid.UUID | None = None
    remember_store: bool = True


class SuggestionOut(_Out):
    ingredient_id: uuid.UUID
    ingredient_name: str
    store_id: uuid.UUID
    store_name: str
    purchases: int


class ItemResult(BaseModel):
    item: ItemOut
    merged: bool = False
    stop_gap: ItemOut | None = None
    prompts: list[str] = []
    suggestions: list[SuggestionOut] = []


class CheckIn(BaseModel):
    store_id: uuid.UUID | None = None
    trip_id: uuid.UUID | None = None


class MealPlanIn(BaseModel):
    start: dt.date | None = None
    end: dt.date | None = None
    entry_ids: list[uuid.UUID] | None = None


class RecipeIn(BaseModel):
    recipe_id: uuid.UUID
    servings: Decimal | None = None


class DismissIn(BaseModel):
    ingredient_id: uuid.UUID
    store_id: uuid.UUID


# ---------------------------------------------------------------- stores
class StoreIn(BaseModel):
    name: str = Field(min_length=1)
    chain: str | None = None
    location_label: str | None = None
    supports_in_store: bool = True
    supports_pickup: bool = False
    supports_delivery: bool = False
    is_bulk: bool = False
    external_list_connector_account_id: uuid.UUID | None = None
    external_list_id: str | None = None


class StorePatch(BaseModel):
    name: str | None = None
    chain: str | None = None
    location_label: str | None = None
    supports_in_store: bool | None = None
    supports_pickup: bool | None = None
    supports_delivery: bool | None = None
    is_bulk: bool | None = None
    external_list_connector_account_id: uuid.UUID | None = None
    external_list_id: str | None = None


class StoreOut(_Out):
    id: uuid.UUID
    name: str
    chain: str | None
    location_label: str | None
    supports_in_store: bool
    supports_pickup: bool
    supports_delivery: bool
    is_bulk: bool
    external_list_connector_account_id: uuid.UUID | None
    external_list_id: str | None


class GroupIn(BaseModel):
    name: str = Field(min_length=1)
    is_default: bool = False
    store_ids: list[uuid.UUID] = []


class GroupPatch(BaseModel):
    name: str | None = None
    is_default: bool | None = None
    store_ids: list[uuid.UUID] | None = None


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    is_default: bool
    store_ids: list[uuid.UUID]


class PrefIn(BaseModel):
    ingredient_id: uuid.UUID
    store_id: uuid.UUID | None = None
    store_group_id: uuid.UUID | None = None
    preferred_search_terms: str | None = None
    preferred_brand: str | None = None
    preferred_size: str | None = None
    note: str | None = None
    typical_quantity: Decimal | None = None
    typical_unit: str | None = None


class HomeIn(BaseModel):
    store_id: uuid.UUID | None = None
    store_group_id: uuid.UUID | None = None


class PrefOut(_Out):
    id: uuid.UUID
    ingredient_id: uuid.UUID
    store_id: uuid.UUID | None
    store_group_id: uuid.UUID | None
    is_home: bool
    preferred_search_terms: str | None
    preferred_brand: str | None
    preferred_size: str | None
    note: str | None
    typical_quantity: Decimal | None
    typical_unit: str | None


# ---------------------------------------------------------------- trips
class TripIn(BaseModel):
    store_id: uuid.UUID
    kind: TripKind
    expected_pickup_at: dt.datetime | None = None


class TripOut(_Out):
    id: uuid.UUID
    store_id: uuid.UUID
    kind: TripKind
    status: TripStatus
    user_id: uuid.UUID | None
    expected_pickup_at: dt.datetime | None
    completed_at: dt.datetime | None
    reminded_at: dt.datetime | None
    created_at: dt.datetime


class TripViewOut(BaseModel):
    trip: TripOut
    store: StoreOut
    get_here: list[ItemOut]
    needed_now: list[ItemOut]
    could_get_here: list[ItemOut]
    on_order: list[ItemOut]
    order_items: list[ItemOut]


class ExportIn(BaseModel):
    item_ids: list[uuid.UUID] = Field(min_length=1)


class ExportLineOut(_Out):
    item_id: uuid.UUID
    display_text: str
    quantity: Decimal | None
    unit: str | None
    is_stop_gap: bool
    search_terms: str | None
    brand: str | None
    size: str | None
    note: str | None


class ExportOut(BaseModel):
    trip: TripOut
    lines: list[ExportLineOut]


class ConfirmIn(BaseModel):
    missing_item_ids: list[uuid.UUID] = []


# ---------------------------------------------------------------- connectors
class ConnectorIn(BaseModel):
    kind: str
    display_name: str | None = None
    base_url: str | None = None
    # Secret: stored encrypted, never returned.
    token: str | None = Field(default=None, repr=False)
    config: dict[str, Any] = {}


class ConnectorPatch(BaseModel):
    display_name: str | None = None
    base_url: str | None = None
    token: str | None = Field(default=None, repr=False)
    config: dict[str, Any] | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class ConnectorOut(_Out):
    id: uuid.UUID
    kind: str
    display_name: str | None
    base_url: str | None
    config: dict[str, Any] | None
    status: str
    has_credentials: bool
    last_synced_at: dt.datetime | None
    last_error: str | None
