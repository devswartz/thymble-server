"""Shopping list, items, item sources and trips (architecture §4, §6.4)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, SyncedBase, str_enum
from thymble.models.stores import STORE_XOR_GROUP


class ItemStatus(enum.StrEnum):
    NEEDED = "needed"
    ON_ORDER = "on_order"
    BOUGHT = "bought"


class Urgency(enum.StrEnum):
    NORMAL = "normal"
    URGENT = "urgent"


class ItemSourceType(enum.StrEnum):
    RECIPE = "recipe"
    MEAL_PLAN_ENTRY = "meal_plan_entry"
    MANUAL = "manual"
    CONNECTOR = "connector"


class TripKind(enum.StrEnum):
    IN_STORE = "in_store"
    PICKUP = "pickup"
    DELIVERY = "delivery"


class TripStatus(enum.StrEnum):
    PLANNING = "planning"
    ON_ORDER = "on_order"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DONE = "done"
    CANCELLED = "cancelled"


class ShoppingList(SyncedBase, Base):
    __tablename__ = "shopping_list"

    name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)


class Trip(SyncedBase, Base):
    """A shopping trip: in-store visit, pickup or delivery order (§6.4)."""

    __tablename__ = "trip"

    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[TripKind] = mapped_column(str_enum(TripKind, "trip_kind"))
    status: Mapped[TripStatus] = mapped_column(
        str_enum(TripStatus, "trip_status"), default=TripStatus.PLANNING
    )
    # Who is shopping / placed the order.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), index=True
    )
    expected_pickup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShoppingItem(SyncedBase, Base):
    __tablename__ = "shopping_item"
    __table_args__ = (CheckConstraint(STORE_XOR_GROUP, name="store_xor_group"),)

    list_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shopping_list.id", ondelete="CASCADE"), index=True
    )
    # NULL for free text that hasn't been parsed/linked yet.
    ingredient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ingredient.id", ondelete="SET NULL"), index=True
    )
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit: Mapped[str | None] = mapped_column(String(32))
    display_text: Mapped[str] = mapped_column(Text)
    status: Mapped[ItemStatus] = mapped_column(
        str_enum(ItemStatus, "shopping_item_status"), default=ItemStatus.NEEDED
    )
    # Set when the item is checked off (status -> bought).
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    # Assignment: a home store or a store group (at most one).
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("store.id", ondelete="SET NULL"), index=True
    )
    store_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("store_group.id", ondelete="SET NULL"), index=True
    )
    urgency: Mapped[Urgency] = mapped_column(
        str_enum(Urgency, "shopping_item_urgency"), default=Urgency.NORMAL
    )
    # Stop-gap item -> the bulk item it bridges (D-029).
    bridge_of: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shopping_item.id", ondelete="SET NULL"), index=True
    )
    # Pickup/delivery order the item is on (D-030).
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trip.id", ondelete="SET NULL"), index=True
    )
    aisle: Mapped[str | None] = mapped_column(String(64))


class ShoppingItemSource(SyncedBase, Base):
    """Where (part of) an item's quantity came from, so merged quantities can un-merge."""

    __tablename__ = "shopping_item_source"

    shopping_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shopping_item.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[ItemSourceType] = mapped_column(
        str_enum(ItemSourceType, "shopping_item_source_type")
    )
    recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipe.id", ondelete="SET NULL"), index=True
    )
    meal_plan_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meal_plan_entry.id", ondelete="SET NULL"), index=True
    )
    connector_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connector_account.id", ondelete="SET NULL"), index=True
    )
    # This source's share of the item quantity.
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit: Mapped[str | None] = mapped_column(String(32))
