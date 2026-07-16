"""Pantry model: what the household already has at home, in purchased grams.

The pantry outlives any single plan — it survives plan deletion and
regeneration. Amounts are stored on the purchased basis (same grams the
basket and meal portions count), keyed by seed-catalog food id.

Two inventories live here:

- ``items`` — catalog foods in grams. Everything downstream (nutrition, meal
  recommendations, recipe matching, purchases, meal draws) reads ONLY this map.
- ``custom_items`` — free-typed products the user saved that did not resolve to
  a catalog food. A pending custom item is inert: it never contributes grams to
  ``items`` and so never touches nutrition, recommendations, matching,
  purchases, or meal draws. It participates only after the user EXPLICITLY links
  it to a catalog ingredient (``link_custom_item``), which is the single moment
  its estimated grams enter ``items``. Linking is never automatic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from models.food import Food
from models.quantities import (
    add_grams,
    canonical_grams,
    normalize_grams,
    subtract_grams,
)

PANTRY_SCHEMA_VERSION = 3

CUSTOM_ID_PREFIX = "custom:"

MAPPING_PENDING = "pending"
MAPPING_LINKED = "linked"
_MAPPING_STATES = (MAPPING_PENDING, MAPPING_LINKED)

_EPSILON = 1e-9


@dataclass
class CustomPantryItem:
    """A user-saved product that did not resolve to a catalog food.

    While ``mapping_status == "pending"`` the item is inventory-inert: it is
    shown on the Pantry page but excluded from every planning/tracking path.
    ``link_custom_item`` on the Pantry is the only way it becomes ``linked``
    and contributes its ``grams_estimate`` to the real pantry.
    """

    id: str  # namespaced "custom:<uuid>" so it can never collide with a food id
    original_name: str  # exactly what the user typed
    display_name: str  # cleaned name shown on the card
    amount: float  # the quantity the user entered (in ``unit``)
    unit: str  # the unit the user entered ("g", "pcs", "can", ...)
    grams_estimate: float  # best-effort grams (0.0 when unknown)
    brand: str = ""
    price: float | None = None
    expiration: str = ""  # ISO date, or "" when unknown
    mapping_status: str = MAPPING_PENDING
    canonical_food_id: str | None = None  # set only once linked
    created_at: str = ""
    image_path: str | None = None
    image_source: str = "placeholder"
    image_source_page: str | None = None
    image_author: str | None = None
    image_license: str | None = None
    image_license_url: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "original_name": self.original_name,
            "display_name": self.display_name,
            "amount": round(float(self.amount), 3),
            "unit": self.unit,
            "grams_estimate": round(float(self.grams_estimate), 3),
            "brand": self.brand,
            "price": (round(float(self.price), 2) if self.price is not None else None),
            "expiration": self.expiration,
            "mapping_status": self.mapping_status,
            "canonical_food_id": self.canonical_food_id,
            "created_at": self.created_at,
            "image_path": self.image_path.replace("\\", "/") if self.image_path else None,
            "image_source": self.image_source,
            "image_source_page": self.image_source_page,
            "image_author": self.image_author,
            "image_license": self.image_license,
            "image_license_url": self.image_license_url,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CustomPantryItem | None":
        """Tolerant reader — a malformed record loads as None (dropped)."""
        try:
            item_id = str(data["id"])
            if not item_id.startswith(CUSTOM_ID_PREFIX):
                return None
            status = str(data.get("mapping_status", MAPPING_PENDING))
            if status not in _MAPPING_STATES:
                status = MAPPING_PENDING
            canonical = data.get("canonical_food_id")
            raw_price = data.get("price")
            image_path = (
                str(data["image_path"]).replace("\\", "/")
                if data.get("image_path") else None
            )
            if image_path and (
                image_path.startswith("/")
                or (len(image_path) > 1 and image_path[1] == ":")
                or ".." in image_path.split("/")
            ):
                image_path = None
            return cls(
                id=item_id,
                original_name=str(data.get("original_name", "")),
                display_name=str(data.get("display_name", data.get("original_name", ""))),
                amount=float(data.get("amount", 0.0) or 0.0),
                unit=str(data.get("unit", "")),
                grams_estimate=max(0.0, float(data.get("grams_estimate", 0.0) or 0.0)),
                brand=str(data.get("brand", "")),
                price=(float(raw_price) if raw_price is not None else None),
                expiration=str(data.get("expiration", "")),
                mapping_status=status,
                canonical_food_id=(str(canonical) if canonical else None),
                created_at=str(data.get("created_at", "")),
                image_path=image_path,
                image_source=str(data.get("image_source", "placeholder")),
                image_source_page=(
                    str(data["image_source_page"]) if data.get("image_source_page") else None
                ),
                image_author=(str(data["image_author"]) if data.get("image_author") else None),
                image_license=(
                    str(data["image_license"]) if data.get("image_license") else None
                ),
                image_license_url=(
                    str(data["image_license_url"])
                    if data.get("image_license_url") else None
                ),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


@dataclass
class Pantry:
    """Mutable food_id -> grams remaining, plus unresolved custom products.

    Empty ``items`` entries are dropped eagerly; ``custom_items`` persist until
    the user removes or links them.
    """

    items: dict[str, float] = field(default_factory=dict)
    custom_items: list[CustomPantryItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        normalized_items: dict[str, float] = {}
        for food_id, grams in self.items.items():
            try:
                normalized = normalize_grams(grams)
            except ValueError:
                continue
            if normalized > 0:
                normalized_items[str(food_id)] = normalized
        self.items = normalized_items

    def add(self, food_id: str, grams: float) -> None:
        try:
            normalized = normalize_grams(grams)
        except ValueError:
            return
        if normalized <= 0:
            return
        self.items[food_id] = add_grams(self.items.get(food_id, 0.0), normalized)

    def remove(self, food_id: str, grams: float) -> float:
        """Remove up to ``grams``, clamped at zero; returns grams actually removed."""
        try:
            normalized = normalize_grams(grams)
        except ValueError:
            return 0.0
        if normalized <= 0:
            return 0.0
        have = normalize_grams(self.items.get(food_id, 0.0))
        removed = min(have, normalized)
        remaining = subtract_grams(have, removed)
        if remaining <= _EPSILON:
            self.items.pop(food_id, None)
        else:
            self.items[food_id] = remaining
        return removed

    def set_grams(self, food_id: str, grams: float) -> None:
        try:
            normalized = normalize_grams(grams)
        except ValueError:
            normalized = 0.0
        if normalized <= 0:
            self.items.pop(food_id, None)
        else:
            self.items[food_id] = normalized

    # -- custom items --------------------------------------------------------

    def add_custom_item(self, item: CustomPantryItem) -> None:
        self.custom_items.append(item)

    def custom_item(self, item_id: str) -> CustomPantryItem | None:
        return next((c for c in self.custom_items if c.id == item_id), None)

    def remove_custom_item(self, item_id: str) -> None:
        self.custom_items = [c for c in self.custom_items if c.id != item_id]

    def pending_custom_items(self) -> list[CustomPantryItem]:
        return [c for c in self.custom_items if c.mapping_status == MAPPING_PENDING]

    def link_custom_item(self, item_id: str, food_id: str) -> bool:
        """Explicitly link a pending custom item to a catalog food.

        This is the ONLY place a custom item's grams enter the planning
        inventory. Idempotent: a second call for the same already-linked item
        does nothing (its grams are not added twice). Returns True on success.
        """
        item = self.custom_item(item_id)
        if item is None or item.mapping_status == MAPPING_LINKED:
            return False
        item.mapping_status = MAPPING_LINKED
        item.canonical_food_id = food_id
        self.add(food_id, item.grams_estimate)
        return True

    def to_dict(self) -> dict:
        return {
            "version": PANTRY_SCHEMA_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "items": {
                fid: canonical_grams(grams)
                for fid, grams in sorted(self.items.items())
            },
            "custom_items": [c.to_dict() for c in self.custom_items],
        }

    @classmethod
    def from_dict(cls, data: dict, foods_by_id: dict[str, Food]) -> "Pantry":
        """Tolerant reader — never None: any corruption loads as an empty pantry,
        unknown food ids and non-positive amounts are dropped silently.

        Accepts v1 (no custom items) and v2. A v1 file simply loads with an
        empty custom-item list; nothing else changes."""
        try:
            if data.get("version") not in (1, 2, PANTRY_SCHEMA_VERSION):
                return cls()
            items: dict[str, float] = {}
            for fid, grams in dict(data.get("items", {})).items():
                fid = str(fid)
                try:
                    grams = normalize_grams(grams)
                except ValueError:
                    continue
                if fid in foods_by_id and grams > 0:
                    items[fid] = grams
            custom_items: list[CustomPantryItem] = []
            for raw in list(data.get("custom_items", [])):
                item = CustomPantryItem.from_dict(dict(raw))
                if item is not None:
                    custom_items.append(item)
            return cls(items=items, custom_items=custom_items)
        except (AttributeError, TypeError, ValueError):
            return cls()
