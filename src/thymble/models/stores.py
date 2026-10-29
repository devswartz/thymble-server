"""Stores, store groups and per-item store preferences (architecture §6.4, D-027/D-028)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Numeric, String, Text, false, true
from sqlalchemy.orm import Mapped, mapped_column

from thymble.db.base import Base, SyncedBase

# Items are assigned to exactly one of: a store, a store group, or nothing yet.
STORE_XOR_GROUP = "store_id IS NULL OR store_group_id IS NULL"


class Store(SyncedBase, Base):
    __tablename__ = "store"

    name: Mapped[str] = mapped_column(Text)
    # Chain identifier, e.g. "walmart", "aldi", "costco".
    chain: Mapped[str | None] = mapped_column(String(64))
    # Human label for the location ("Main St"); not retailer location data (D-023).
    location_label: Mapped[str | None] = mapped_column(Text)
    supports_in_store: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    supports_pickup: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    supports_delivery: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    is_bulk: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    # Optional linked external list (e.g. the Costco Alexa list via Home Assistant).
    external_list_connector_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connector_account.id", ondelete="SET NULL")
    )
    external_list_id: Mapped[str | None] = mapped_column(Text)


class StoreGroup(SyncedBase, Base):
    """E.g. "Everyday grocery" = Aldi + Walmart."""

    __tablename__ = "store_group"

    name: Mapped[str] = mapped_column(Text)


class StoreGroupMember(SyncedBase, Base):
    __tablename__ = "store_group_member"

    store_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store_group.id", ondelete="CASCADE"), index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), index=True
    )


class ItemStorePref(SyncedBase, Base):
    """A household's preference for one item at one store or store group.

    One row per (item, store-or-group). `is_home` marks the item's home; other rows are
    places where it is also OK. Product preference is stored as our own data only
    (search terms, brand, size, note) — no retailer product ids until O-7 is answered (D-023).
    """

    __tablename__ = "item_store_pref"
    __table_args__ = (
        CheckConstraint(STORE_XOR_GROUP, name="store_xor_group"),
        CheckConstraint(
            "store_id IS NOT NULL OR store_group_id IS NOT NULL", name="store_or_group"
        ),
    )

    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredient.id", ondelete="CASCADE"), index=True
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("store.id", ondelete="CASCADE"), index=True
    )
    store_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("store_group.id", ondelete="CASCADE"), index=True
    )
    is_home: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    preferred_search_terms: Mapped[str | None] = mapped_column(Text)
    preferred_brand: Mapped[str | None] = mapped_column(Text)
    preferred_size: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    # Bulk vs stop-gap amounts differ per store.
    typical_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    typical_unit: Mapped[str | None] = mapped_column(String(32))
