"""All ORM models. Importing this package registers every table on `Base.metadata`."""

from thymble.db.base import Base
from thymble.models.auth import EmailLoginToken, HouseholdInvite, RefreshToken
from thymble.models.catalog import Ingredient, IngredientAlias
from thymble.models.connectors import ConnectorAccount, ConnectorItemLink
from thymble.models.identity import Household, HouseholdMember, HouseholdRole, User
from thymble.models.imports import ImportJob, ImportSourceKind, ImportStatus, LlmUsage
from thymble.models.learning import LearningEvent, LlmCache
from thymble.models.planning import MealPlanEntry, MealSlot
from thymble.models.recipes import (
    Recipe,
    RecipeIngredient,
    RecipeSourceType,
    RecipeStep,
    RecipeTag,
    Tag,
)
from thymble.models.shopping import (
    ItemSourceType,
    ItemStatus,
    ShoppingItem,
    ShoppingItemSource,
    ShoppingList,
    Trip,
    TripKind,
    TripStatus,
    Urgency,
)
from thymble.models.stores import ItemStorePref, Store, StoreGroup, StoreGroupMember

__all__ = [
    "Base",
    "ConnectorAccount",
    "ConnectorItemLink",
    "EmailLoginToken",
    "Household",
    "HouseholdInvite",
    "HouseholdMember",
    "HouseholdRole",
    "ImportJob",
    "ImportSourceKind",
    "ImportStatus",
    "Ingredient",
    "IngredientAlias",
    "ItemSourceType",
    "ItemStatus",
    "ItemStorePref",
    "LearningEvent",
    "LlmCache",
    "LlmUsage",
    "MealPlanEntry",
    "MealSlot",
    "Recipe",
    "RecipeIngredient",
    "RecipeSourceType",
    "RecipeStep",
    "RecipeTag",
    "RefreshToken",
    "ShoppingItem",
    "ShoppingItemSource",
    "ShoppingList",
    "Store",
    "StoreGroup",
    "StoreGroupMember",
    "Tag",
    "Trip",
    "TripKind",
    "TripStatus",
    "Urgency",
    "User",
]
