"""Shopping list domain logic (architecture §6.1, §6.4; D-027 to D-030).

`ShoppingService` works on one household inside one SQLAlchemy session. The REST api,
the Home Assistant connector and worker jobs all go through it, so list semantics
(merging, remembered store assignment, urgency and stop-gaps, orders) live in one place.

The caller owns the transaction: methods flush but never commit. Every item a method
changes is added to `touched`, so connectors know what to mirror.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from thymble import units
from thymble.models import (
    Ingredient,
    IngredientAlias,
    ItemSourceType,
    ItemStatus,
    ItemStorePref,
    LearningEvent,
    ShoppingItem,
    ShoppingItemSource,
    ShoppingList,
    Store,
    StoreGroup,
    StoreGroupMember,
    Trip,
    Urgency,
)
from thymble.shopping.errors import NotFound, ShoppingError
from thymble.shopping.text import STOPGAP_SUFFIX, name_key, name_variants


def _store_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


# Purchases at one store outside the item's home before Thymble asks
# "make this the usual store?" (§6.4).
HOME_SUGGESTION_THRESHOLD = 3

EV_ITEM_CHECKED = "item_checked"
EV_HOME_SET = "item_home_set"
EV_HOME_SUGGESTION_DISMISSED = "item_home_suggestion_dismissed"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AddResult:
    item: ShoppingItem
    merged: bool


@dataclass
class HomeSuggestion:
    ingredient_id: uuid.UUID
    ingredient_name: str
    store_id: uuid.UUID
    store_name: str
    purchases: int


@dataclass
class Assignment:
    store_id: uuid.UUID | None = None
    store_group_id: uuid.UUID | None = None

    @property
    def empty(self) -> bool:
        return self.store_id is None and self.store_group_id is None


@dataclass
class ShoppingService:
    session: Session
    household_id: uuid.UUID
    user_id: uuid.UUID | None = None
    touched: set[uuid.UUID] = field(default_factory=set)
    prompts: list[str] = field(default_factory=list)
    suggestions: list[HomeSuggestion] = field(default_factory=list)

    # ------------------------------------------------------------------ loading
    def _get(self, model, obj_id: uuid.UUID):
        obj = self.session.get(model, obj_id)
        if obj is None or obj.household_id != self.household_id or obj.deleted_at is not None:
            raise NotFound(f"{model.__tablename__} {obj_id} not found")
        return obj

    def get_list(self, list_id: uuid.UUID) -> ShoppingList:
        return self._get(ShoppingList, list_id)

    def get_item(self, item_id: uuid.UUID) -> ShoppingItem:
        return self._get(ShoppingItem, item_id)

    def get_store(self, store_id: uuid.UUID) -> Store:
        return self._get(Store, store_id)

    def get_group(self, group_id: uuid.UUID) -> StoreGroup:
        return self._get(StoreGroup, group_id)

    def get_trip(self, trip_id: uuid.UUID) -> Trip:
        return self._get(Trip, trip_id)

    def default_list(self) -> ShoppingList:
        lst = self.session.scalars(
            select(ShoppingList)
            .where(
                ShoppingList.household_id == self.household_id,
                ShoppingList.deleted_at.is_(None),
                ShoppingList.is_active.is_(True),
            )
            .order_by(ShoppingList.created_at, ShoppingList.id)
            .limit(1)
        ).first()
        if lst is None:
            lst = ShoppingList(household_id=self.household_id, name="Shopping")
            self.session.add(lst)
            self.session.flush()
        return lst

    def items(self, list_id: uuid.UUID, statuses: Iterable[ItemStatus] | None = None):
        q = select(ShoppingItem).where(
            ShoppingItem.list_id == list_id,
            ShoppingItem.household_id == self.household_id,
            ShoppingItem.deleted_at.is_(None),
        )
        if statuses is not None:
            q = q.where(ShoppingItem.status.in_(list(statuses)))
        return list(self.session.scalars(q.order_by(ShoppingItem.created_at, ShoppingItem.id)))

    def _touch(self, item: ShoppingItem) -> None:
        if item.id is None:
            self.session.flush()
        self.touched.add(item.id)

    # ------------------------------------------------------------- ingredients
    def resolve_ingredient(self, name: str) -> Ingredient | None:
        """Match a name to a catalog ingredient (household first, then global, then
        aliases); create a household ingredient when nothing matches.

        Stand-in for the normalization step of the import pipeline (§5 step 6).
        """
        key = name_key(name)
        if not key:
            return None
        variants = name_variants(name)
        scope = or_(Ingredient.household_id == self.household_id, Ingredient.household_id.is_(None))
        candidates = self.session.scalars(
            select(Ingredient).where(
                scope, Ingredient.deleted_at.is_(None), func.lower(Ingredient.name).in_(variants)
            )
        ).all()
        exact = [c for c in candidates if name_key(c.name) == key]
        if exact:
            exact.sort(key=lambda c: c.household_id is None)  # household rows win
            return exact[0]
        alias = self.session.scalars(
            select(IngredientAlias).where(
                or_(
                    IngredientAlias.household_id == self.household_id,
                    IngredientAlias.household_id.is_(None),
                ),
                IngredientAlias.deleted_at.is_(None),
                func.lower(IngredientAlias.alias).in_(variants),
            )
        ).first()
        if alias is not None:
            ing = self.session.get(Ingredient, alias.ingredient_id)
            if ing is not None and ing.deleted_at is None:
                return ing
        ing = Ingredient(household_id=self.household_id, name=" ".join(name.strip().split()))
        self.session.add(ing)
        self.session.flush()
        return ing

    def get_ingredient(self, ingredient_id: uuid.UUID) -> Ingredient:
        ing = self.session.get(Ingredient, ingredient_id)
        if (
            ing is None
            or ing.deleted_at is not None
            or ing.household_id not in (None, self.household_id)
        ):
            raise NotFound(f"ingredient {ingredient_id} not found")
        return ing

    # -------------------------------------------------------------- stores
    def _stores(self) -> list[Store]:
        return list(
            self.session.scalars(
                select(Store)
                .where(Store.household_id == self.household_id, Store.deleted_at.is_(None))
                .order_by(Store.created_at)
            )
        )

    def store_names(self) -> list[str]:
        """The household's store names and chains, for store hints in voice entries."""
        return [n for s in self._stores() for n in (s.name, s.chain) if n]

    def store_for_hint(self, hint: str | None) -> Store | None:
        """The household's store a voice entry named ("bread from aldi"), if it has one."""
        if not hint:
            return None
        key = _store_key(hint)
        for store in self._stores():
            if key in {_store_key(store.name), _store_key(store.chain or "")}:
                return store
        return None

    def group_store_ids(self, group_id: uuid.UUID) -> set[uuid.UUID]:
        return set(
            self.session.scalars(
                select(StoreGroupMember.store_id).where(
                    StoreGroupMember.store_group_id == group_id,
                    StoreGroupMember.deleted_at.is_(None),
                )
            )
        )

    def groups_containing(self, store_id: uuid.UUID) -> set[uuid.UUID]:
        return set(
            self.session.scalars(
                select(StoreGroupMember.store_group_id).where(
                    StoreGroupMember.store_id == store_id,
                    StoreGroupMember.deleted_at.is_(None),
                )
            )
        )

    def assignment_is_bulk(self, store_id: uuid.UUID | None, group_id: uuid.UUID | None) -> bool:
        if store_id is not None:
            store = self.session.get(Store, store_id)
            return bool(store and store.is_bulk)
        if group_id is not None:
            ids = self.group_store_ids(group_id)
            if not ids:
                return False
            stores = self.session.scalars(select(Store).where(Store.id.in_(ids))).all()
            return all(s.is_bulk for s in stores)
        return False

    def assignment_covers_store(
        self, store_id: uuid.UUID | None, group_id: uuid.UUID | None, store: uuid.UUID
    ) -> bool:
        if store_id is not None:
            return store_id == store
        if group_id is not None:
            return store in self.group_store_ids(group_id)
        return False

    def _check_assignment(self, store_id: uuid.UUID | None, group_id: uuid.UUID | None) -> None:
        if store_id is not None and group_id is not None:
            raise ShoppingError("assign either a store or a store group, not both")
        if store_id is not None:
            self.get_store(store_id)
        if group_id is not None:
            self.get_group(group_id)

    # --------------------------------------------------------- item prefs
    def _prefs(self, ingredient_id: uuid.UUID) -> list[ItemStorePref]:
        return list(
            self.session.scalars(
                select(ItemStorePref)
                .where(
                    ItemStorePref.household_id == self.household_id,
                    ItemStorePref.ingredient_id == ingredient_id,
                    ItemStorePref.deleted_at.is_(None),
                )
                .order_by(ItemStorePref.created_at, ItemStorePref.id)
            )
        )

    def home_of(self, ingredient_id: uuid.UUID | None) -> Assignment | None:
        if ingredient_id is None:
            return None
        for pref in self._prefs(ingredient_id):
            if pref.is_home:
                return Assignment(pref.store_id, pref.store_group_id)
        return None

    def pref_for(
        self, ingredient_id: uuid.UUID, store_id: uuid.UUID | None, group_id: uuid.UUID | None
    ) -> ItemStorePref | None:
        for pref in self._prefs(ingredient_id):
            if pref.store_id == store_id and pref.store_group_id == group_id:
                return pref
        return None

    def upsert_pref(
        self,
        ingredient_id: uuid.UUID,
        store_id: uuid.UUID | None = None,
        group_id: uuid.UUID | None = None,
        **details,
    ) -> ItemStorePref:
        """Create or update the (item, store-or-group) preference row."""
        self.get_ingredient(ingredient_id)
        self._check_assignment(store_id, group_id)
        if store_id is None and group_id is None:
            raise ShoppingError("a preference needs a store or a store group")
        pref = self.pref_for(ingredient_id, store_id, group_id)
        if pref is None:
            pref = ItemStorePref(
                household_id=self.household_id,
                ingredient_id=ingredient_id,
                store_id=store_id,
                store_group_id=group_id,
                is_home=False,
            )
            self.session.add(pref)
        for key, value in details.items():
            setattr(pref, key, value)
        self.session.flush()
        return pref

    def set_home(
        self, ingredient_id: uuid.UUID, store_id: uuid.UUID | None, group_id: uuid.UUID | None
    ) -> ItemStorePref | None:
        """Remember where this item normally comes from (None, None clears it)."""
        self.get_ingredient(ingredient_id)
        self._check_assignment(store_id, group_id)
        current = self.home_of(ingredient_id)
        if current is not None and (current.store_id, current.store_group_id) == (
            store_id,
            group_id,
        ):
            return self.pref_for(ingredient_id, store_id, group_id)
        for pref in self._prefs(ingredient_id):
            if pref.is_home:
                pref.is_home = False
        pref = None
        if store_id is not None or group_id is not None:
            pref = self.upsert_pref(ingredient_id, store_id, group_id, is_home=True)
        self._event(
            EV_HOME_SET,
            ingredient_id,
            {
                "store_id": _s(store_id),
                "store_group_id": _s(group_id),
            },
        )
        self.session.flush()
        return pref

    def _event(self, event_type: str, ingredient_id: uuid.UUID | None, payload: dict) -> None:
        self.session.add(
            LearningEvent(
                household_id=self.household_id,
                user_id=self.user_id,
                event_type=event_type,
                subject_type="ingredient",
                subject_id=ingredient_id,
                payload=payload,
                occurred_at=utcnow(),
            )
        )

    # -------------------------------------------------------------- queries
    def find_open(
        self,
        list_id: uuid.UUID,
        ingredient_id: uuid.UUID | None,
        name: str,
        quantity: Decimal | None = None,
        unit: str | None = None,
    ) -> ShoppingItem | None:
        """An unchecked (needed), non-stop-gap item this add can merge into."""
        q = select(ShoppingItem).where(
            ShoppingItem.list_id == list_id,
            ShoppingItem.household_id == self.household_id,
            ShoppingItem.deleted_at.is_(None),
            ShoppingItem.status == ItemStatus.NEEDED,
            ShoppingItem.bridge_of.is_(None),
        )
        if ingredient_id is not None:
            q = q.where(ShoppingItem.ingredient_id == ingredient_id)
        else:
            q = q.where(ShoppingItem.ingredient_id.is_(None))
        for item in self.session.scalars(q.order_by(ShoppingItem.created_at, ShoppingItem.id)):
            if ingredient_id is None and name_key(item.display_text) != name_key(name):
                continue
            if units.mergeable(item.quantity, item.unit, quantity, unit):
                return item
        return None

    def stop_gaps(self, bulk: ShoppingItem, *, open_only: bool = True) -> list[ShoppingItem]:
        q = select(ShoppingItem).where(
            ShoppingItem.bridge_of == bulk.id, ShoppingItem.deleted_at.is_(None)
        )
        if open_only:
            q = q.where(ShoppingItem.status != ItemStatus.BOUGHT)
        return list(self.session.scalars(q.order_by(ShoppingItem.created_at)))

    def mirror_text(self, item: ShoppingItem) -> str:
        """Human-readable text for external lists (§6.3): '2 lb chicken thighs'."""
        qty = units.format_quantity(item.quantity, item.unit)
        if item.bridge_of is not None:
            text = item.display_text + STOPGAP_SUFFIX
        else:
            text = f"{qty} {item.display_text}" if qty else item.display_text
        if item.status == ItemStatus.ON_ORDER and item.trip_id is not None:
            trip = self.session.get(Trip, item.trip_id)
            store = self.session.get(Store, trip.store_id) if trip else None
            if store is not None:
                text += f" — on {store.name} order"
        return text

    # ------------------------------------------------------------ editing
    def add_item(
        self,
        list_id: uuid.UUID,
        name: str,
        quantity: Decimal | None = None,
        unit: str | None = None,
        *,
        store_id: uuid.UUID | None = None,
        store_group_id: uuid.UUID | None = None,
        remember: bool = True,
        urgent: bool = False,
        source_type: ItemSourceType = ItemSourceType.MANUAL,
        recipe_id: uuid.UUID | None = None,
        meal_plan_entry_id: uuid.UUID | None = None,
        connector_account_id: uuid.UUID | None = None,
        ingredient_id: uuid.UUID | None = None,
        resolve: bool = True,
        merge: bool = True,
    ) -> AddResult:
        """Add an item, merging into an open item of the same ingredient (§6.3 duplicates).

        An explicit store / group is remembered as the item's home; otherwise the
        remembered home is used (§6.4 "Assignment").
        """
        self.get_list(list_id)
        self._check_assignment(store_id, store_group_id)
        name = " ".join(name.strip().split())
        if not name:
            raise ShoppingError("item name is empty")
        unit = units.normalize(unit)
        quantity = Decimal(quantity) if quantity is not None else None
        if ingredient_id is None and resolve:
            ing = self.resolve_ingredient(name)
            ingredient_id = ing.id if ing else None
        explicit = store_id is not None or store_group_id is not None
        if explicit and remember and ingredient_id is not None:
            self.set_home(ingredient_id, store_id, store_group_id)

        existing = self.find_open(list_id, ingredient_id, name, quantity, unit) if merge else None
        if existing is not None:
            existing.quantity, existing.unit = units.merge(
                existing.quantity, existing.unit, quantity, unit
            )
            if explicit:
                existing.store_id, existing.store_group_id = store_id, store_group_id
            self._add_source(
                existing,
                source_type,
                quantity,
                unit,
                recipe_id,
                meal_plan_entry_id,
                connector_account_id,
            )
            self._touch(existing)
            if urgent:
                self.set_urgent(existing, True)
            elif existing.urgency != Urgency.URGENT and self.assignment_is_bulk(
                existing.store_id, existing.store_group_id
            ):
                # §6.4: re-adding something already waiting on the bulk list asks instead.
                self.prompts.append(f"Need some {existing.display_text} sooner?")
            return AddResult(existing, merged=True)

        assignment = Assignment(store_id, store_group_id)
        if not explicit:
            assignment = self.home_of(ingredient_id) or Assignment()
        item = ShoppingItem(
            household_id=self.household_id,
            list_id=list_id,
            ingredient_id=ingredient_id,
            display_text=name,
            quantity=quantity,
            unit=unit,
            status=ItemStatus.NEEDED,
            urgency=Urgency.NORMAL,
            store_id=assignment.store_id,
            store_group_id=assignment.store_group_id,
        )
        self.session.add(item)
        self.session.flush()
        self._add_source(
            item, source_type, quantity, unit, recipe_id, meal_plan_entry_id, connector_account_id
        )
        self._touch(item)
        if urgent:
            self.set_urgent(item, True)
        return AddResult(item, merged=False)

    def _add_source(
        self,
        item: ShoppingItem,
        source_type: ItemSourceType,
        quantity: Decimal | None,
        unit: str | None,
        recipe_id: uuid.UUID | None,
        meal_plan_entry_id: uuid.UUID | None,
        connector_account_id: uuid.UUID | None,
    ) -> None:
        self.session.add(
            ShoppingItemSource(
                household_id=self.household_id,
                shopping_item_id=item.id,
                source_type=source_type,
                quantity=quantity,
                unit=unit,
                recipe_id=recipe_id,
                meal_plan_entry_id=meal_plan_entry_id,
                connector_account_id=connector_account_id,
            )
        )

    def update_item(
        self,
        item: ShoppingItem,
        *,
        display_text: str | None = None,
        quantity: Decimal | None = None,
        unit: str | None = None,
        clear_quantity: bool = False,
        aisle: str | None = None,
    ) -> ShoppingItem:
        if display_text is not None and display_text.strip():
            new = " ".join(display_text.split())
            if name_key(new) != name_key(item.display_text):
                ing = self.resolve_ingredient(new)
                item.ingredient_id = ing.id if ing else None
            item.display_text = new
        if clear_quantity:
            item.quantity, item.unit = None, None
        elif quantity is not None:
            item.quantity, item.unit = quantity, units.normalize(unit)
        if aisle is not None:
            item.aisle = aisle or None
        self._touch(item)
        return item

    def assign(
        self,
        item: ShoppingItem,
        store_id: uuid.UUID | None,
        group_id: uuid.UUID | None,
        *,
        remember: bool = True,
    ) -> ShoppingItem:
        self._check_assignment(store_id, group_id)
        item.store_id, item.store_group_id = store_id, group_id
        if remember and item.ingredient_id is not None and item.bridge_of is None:
            self.set_home(item.ingredient_id, store_id, group_id)
        self._touch(item)
        if item.urgency == Urgency.URGENT:
            self.set_urgent(item, True)  # a newly bulk home may need a stop-gap
        return item

    # ---------------------------------------------------- urgency (D-029)
    def stopgap_assignment(self, bulk: ShoppingItem) -> tuple[Assignment, ItemStorePref | None]:
        """Where a stop-gap goes: a non-bulk "also OK" place, else the default group."""
        if bulk.ingredient_id is not None:
            for pref in self._prefs(bulk.ingredient_id):
                if pref.is_home:
                    continue
                if not self.assignment_is_bulk(pref.store_id, pref.store_group_id):
                    return Assignment(pref.store_id, pref.store_group_id), pref
        group = self.session.scalars(
            select(StoreGroup)
            .where(
                StoreGroup.household_id == self.household_id,
                StoreGroup.deleted_at.is_(None),
                StoreGroup.is_default.is_(True),
            )
            .order_by(StoreGroup.created_at)
            .limit(1)
        ).first()
        if group is None:
            return Assignment(), None
        pref = (
            self.pref_for(bulk.ingredient_id, None, group.id)
            if bulk.ingredient_id is not None
            else None
        )
        return Assignment(None, group.id), pref

    def set_urgent(self, item: ShoppingItem, urgent: bool) -> ShoppingItem | None:
        """Mark urgent / not urgent. Returns the stop-gap when one exists afterwards."""
        item.urgency = Urgency.URGENT if urgent else Urgency.NORMAL
        self._touch(item)
        if item.bridge_of is not None:
            return None
        gaps = self.stop_gaps(item)
        if not urgent:
            for gap in gaps:
                self.delete_item(gap)
            return None
        if gaps:
            return gaps[0]
        if item.status != ItemStatus.NEEDED or not self.assignment_is_bulk(
            item.store_id, item.store_group_id
        ):
            return None
        assignment, pref = self.stopgap_assignment(item)
        gap = ShoppingItem(
            household_id=self.household_id,
            list_id=item.list_id,
            ingredient_id=item.ingredient_id,
            display_text=item.display_text,
            quantity=pref.typical_quantity if pref else None,
            unit=pref.typical_unit if pref else None,
            status=ItemStatus.NEEDED,
            urgency=Urgency.URGENT,
            store_id=assignment.store_id,
            store_group_id=assignment.store_group_id,
            bridge_of=item.id,
            aisle=item.aisle,
        )
        self.session.add(gap)
        self.session.flush()
        self._touch(gap)
        return gap

    # ------------------------------------------------------- status changes
    def mark_bought(
        self,
        item: ShoppingItem,
        *,
        store_id: uuid.UUID | None = None,
        trip_id: uuid.UUID | None = None,
    ) -> ShoppingItem:
        """Check an item off. Stop-gap bought: the bulk item stays, no longer urgent.
        Bulk item bought: its stop-gaps are bought too (D-029)."""
        if item.status == ItemStatus.BOUGHT:
            return item
        item.status = ItemStatus.BOUGHT
        item.checked_at = utcnow()
        item.checked_by = self.user_id
        item.urgency = Urgency.NORMAL
        if trip_id is not None:
            item.trip_id = trip_id
        self._touch(item)
        if item.bridge_of is not None:
            bulk = self.session.get(ShoppingItem, item.bridge_of)
            if bulk is not None and bulk.urgency == Urgency.URGENT:
                bulk.urgency = Urgency.NORMAL
                self._touch(bulk)
        else:
            for gap in self.stop_gaps(item):
                self.mark_bought(gap, trip_id=trip_id)
            if store_id is not None and item.ingredient_id is not None:
                self._learn_checkoff(item, store_id, trip_id)
        return item

    def mark_needed(self, item: ShoppingItem) -> ShoppingItem:
        if item.status == ItemStatus.NEEDED:
            return item
        item.status = ItemStatus.NEEDED
        item.checked_at = None
        item.checked_by = None
        item.trip_id = None
        self._touch(item)
        return item

    def delete_item(self, item: ShoppingItem) -> None:
        if item.deleted_at is not None:
            return
        for gap in self.stop_gaps(item, open_only=False):
            self.delete_item(gap)
        item.deleted_at = utcnow()
        self._touch(item)

    # ---------------------------------------------------------- learning
    def _learn_checkoff(
        self, item: ShoppingItem, store_id: uuid.UUID, trip_id: uuid.UUID | None
    ) -> None:
        home = self.home_of(item.ingredient_id)
        at_home = home is not None and self.assignment_covers_store(
            home.store_id, home.store_group_id, store_id
        )
        self._event(
            EV_ITEM_CHECKED,
            item.ingredient_id,
            {
                "store_id": str(store_id),
                "trip_id": _s(trip_id),
                "shopping_item_id": str(item.id),
                "at_home": at_home,
            },
        )
        if at_home:
            return
        # Bought elsewhere: remember it is also OK there (§6.4 "Check-offs teach it").
        if home is not None and self.pref_for(item.ingredient_id, store_id, None) is None:
            self.upsert_pref(item.ingredient_id, store_id, None)
        self.session.flush()
        suggestion = self.home_suggestion(item.ingredient_id, store_id)
        if suggestion is not None:
            self.suggestions.append(suggestion)

    def home_suggestion(
        self, ingredient_id: uuid.UUID, store_id: uuid.UUID
    ) -> HomeSuggestion | None:
        """'Make this the usual store?' after enough purchases there since the home was
        last set (or the suggestion last dismissed)."""
        home = self.home_of(ingredient_id)
        if home is not None and home.store_id == store_id:
            return None
        since = self.session.scalar(
            select(func.max(LearningEvent.occurred_at)).where(
                LearningEvent.household_id == self.household_id,
                LearningEvent.subject_id == ingredient_id,
                or_(
                    LearningEvent.event_type == EV_HOME_SET,
                    and_(
                        LearningEvent.event_type == EV_HOME_SUGGESTION_DISMISSED,
                        LearningEvent.payload["store_id"].astext == str(store_id),
                    ),
                ),
            )
        )
        q = select(func.count()).where(
            LearningEvent.household_id == self.household_id,
            LearningEvent.event_type == EV_ITEM_CHECKED,
            LearningEvent.subject_id == ingredient_id,
            LearningEvent.payload["store_id"].astext == str(store_id),
            LearningEvent.payload["at_home"].astext == "false",
            LearningEvent.deleted_at.is_(None),
        )
        if since is not None:
            q = q.where(LearningEvent.occurred_at > since)
        count = self.session.scalar(q) or 0
        if count < HOME_SUGGESTION_THRESHOLD:
            return None
        ing = self.session.get(Ingredient, ingredient_id)
        store = self.session.get(Store, store_id)
        return HomeSuggestion(
            ingredient_id=ingredient_id,
            ingredient_name=ing.name if ing else "",
            store_id=store_id,
            store_name=store.name if store else "",
            purchases=count,
        )

    def pending_home_suggestions(self) -> list[HomeSuggestion]:
        pairs = self.session.execute(
            select(LearningEvent.subject_id, LearningEvent.payload["store_id"].astext)
            .where(
                LearningEvent.household_id == self.household_id,
                LearningEvent.event_type == EV_ITEM_CHECKED,
                LearningEvent.payload["at_home"].astext == "false",
                LearningEvent.deleted_at.is_(None),
            )
            .distinct()
        ).all()
        out = []
        for ingredient_id, store_id in pairs:
            if ingredient_id is None or not store_id:
                continue
            suggestion = self.home_suggestion(ingredient_id, uuid.UUID(store_id))
            if suggestion is not None:
                out.append(suggestion)
        return out

    def dismiss_home_suggestion(self, ingredient_id: uuid.UUID, store_id: uuid.UUID) -> None:
        self._event(EV_HOME_SUGGESTION_DISMISSED, ingredient_id, {"store_id": str(store_id)})
        self.session.flush()


def _s(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None
