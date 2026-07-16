"""Purchase events: the source of truth for what was bought.

Every purchase — the Purchased button, a product photo, a receipt line, or a
legacy migration — is one immutable PurchaseRecord. ``plan.purchased`` is an
aggregate CACHE derived from these records (rebuild_purchase_aggregates);
undo VOIDS a record (``voided_at``), it never deletes one, so history stays
auditable. ``pantry_grams_before`` is read by the purchase service at
mutation time — callers never construct it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from models.quantities import (
    canonical_grams,
    canonical_money,
    canonical_quantity,
    normalize_grams,
    normalize_money,
    normalize_quantity,
)

PURCHASE_LOG_SCHEMA_VERSION = 4

ORIGIN_DIRECT_BUTTON = "direct_button"
ORIGIN_PRODUCT_PHOTO = "product_photo"
ORIGIN_RECEIPT = "receipt"
ORIGIN_LEGACY_MIGRATION = "legacy_migration"
ORIGINS = (
    ORIGIN_DIRECT_BUTTON,
    ORIGIN_PRODUCT_PHOTO,
    ORIGIN_RECEIPT,
    ORIGIN_LEGACY_MIGRATION,
)

PRICE_SOURCE_VISIBLE = "visible_on_product"
PRICE_SOURCE_RECEIPT = "receipt_line"
PRICE_SOURCE_USER = "user_entered"
PRICE_SOURCE_UNKNOWN = "unknown"
PRICE_SOURCES = (
    PRICE_SOURCE_VISIBLE,
    PRICE_SOURCE_RECEIPT,
    PRICE_SOURCE_USER,
    PRICE_SOURCE_UNKNOWN,
)

GRAMS_SOURCE_VISIBLE_TOTAL = "visible_total"
GRAMS_SOURCE_VISIBLE_UNIT_TIMES_QUANTITY = "visible_unit_times_quantity"
GRAMS_SOURCE_DESCRIPTION_PARSED = "description_parsed"
GRAMS_SOURCE_CATALOG_ESTIMATE = "catalog_estimate"
GRAMS_SOURCE_USER_ENTERED = "user_entered"
GRAMS_SOURCES = (
    GRAMS_SOURCE_VISIBLE_TOTAL,
    GRAMS_SOURCE_VISIBLE_UNIT_TIMES_QUANTITY,
    GRAMS_SOURCE_DESCRIPTION_PARSED,
    GRAMS_SOURCE_CATALOG_ESTIMATE,
    GRAMS_SOURCE_USER_ENTERED,
)


def new_purchase_event_id() -> str:
    """Pre-allocated BEFORE any image write or record creation — the photo
    file is named after it, so the file can exist before the record does."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class PurchaseInput:
    """What the UI/confirm dialog produces — no baseline, no timestamps.
    The purchase service converts this into the persisted PurchaseRecord."""

    event_id: str  # from new_purchase_event_id(); also names the photo file
    food_id: str  # ALWAYS a catalog food — no record without a chosen food
    raw_name: str = ""
    brand: str | None = None
    package_label: str | None = None
    grams: float = 0.0  # final total grams this line adds
    quantity: float = 1.0
    line_total: float | None = None  # confirmed line TOTAL — feeds Actual spent
    estimated_line_cost: float | None = None  # estimate — feeds Estimated purchased
    price_source: str = PRICE_SOURCE_UNKNOWN
    store: str = ""
    photo_path: str | None = None  # RELATIVE to the profile dir
    apply_to_plan: bool = False  # input-only; becomes plan_id on the record
    group_id: str = ""  # one user action (button press / one receipt)
    origin: str = ORIGIN_DIRECT_BUTTON
    grams_source: str = GRAMS_SOURCE_USER_ENTERED
    source_line_index: int | None = None
    segment_index: int | None = None
    currency: str | None = None
    # Set only when the user buys one explicit planned basket child row.
    # Receipt/product imports remain unlinked unless the user explicitly chose
    # that row; package_id may still describe the package actually purchased.
    basket_item_id: str | None = None
    package_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "grams", normalize_grams(self.grams))
        object.__setattr__(self, "quantity", normalize_quantity(self.quantity))
        if self.line_total is not None:
            object.__setattr__(
                self, "line_total", normalize_money(self.line_total, positive=True)
            )
        if self.estimated_line_cost is not None:
            object.__setattr__(
                self,
                "estimated_line_cost",
                normalize_money(self.estimated_line_cost),
            )
        if self.currency is not None:
            object.__setattr__(self, "currency", self.currency.strip().upper() or None)
        for name in ("basket_item_id", "package_id"):
            value = getattr(self, name)
            normalized = str(value).strip() if value is not None else ""
            object.__setattr__(self, name, normalized or None)


@dataclass(frozen=True)
class PurchaseRecord:
    """A persisted purchase fact — constructed by the service only."""

    event_id: str
    food_id: str
    raw_name: str
    brand: str | None
    package_label: str | None
    grams: float
    quantity: float
    line_total: float | None
    estimated_line_cost: float | None
    price_source: str
    store: str
    photo_path: str | None
    group_id: str
    origin: str
    purchased_at: str  # ISO, set by the service
    plan_id: str | None  # THE single apply-to-plan fact (None = off-plan)
    pantry_grams_before: float  # stock waterline before this event — undo guard
    voided_at: str | None = None  # undo marks, never deletes
    grams_source: str = GRAMS_SOURCE_USER_ENTERED
    source_line_index: int | None = None
    segment_index: int | None = None
    currency: str | None = None
    basket_item_id: str | None = None
    package_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "grams", normalize_grams(self.grams, positive=True))
        object.__setattr__(self, "quantity", normalize_quantity(self.quantity))
        object.__setattr__(
            self,
            "pantry_grams_before",
            normalize_grams(self.pantry_grams_before),
        )
        if self.line_total is not None:
            object.__setattr__(
                self, "line_total", normalize_money(self.line_total, positive=True)
            )
        if self.estimated_line_cost is not None:
            object.__setattr__(
                self,
                "estimated_line_cost",
                normalize_money(self.estimated_line_cost),
            )
        if self.currency is not None:
            object.__setattr__(self, "currency", self.currency.strip().upper() or None)
        for name in ("basket_item_id", "package_id"):
            value = getattr(self, name)
            normalized = str(value).strip() if value is not None else ""
            object.__setattr__(self, name, normalized or None)
        if self.basket_item_id is not None and self.package_id is None:
            raise ValueError("a linked purchase record needs a package_id")

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "food_id": self.food_id,
            "raw_name": self.raw_name,
            "brand": self.brand,
            "package_label": self.package_label,
            "grams": canonical_grams(self.grams),
            "quantity": canonical_quantity(self.quantity),
            "line_total": (
                canonical_money(self.line_total) if self.line_total is not None else None
            ),
            "estimated_line_cost": (
                canonical_money(self.estimated_line_cost)
                if self.estimated_line_cost is not None else None
            ),
            "price_source": self.price_source,
            "store": self.store,
            "photo_path": self.photo_path,
            "group_id": self.group_id,
            "origin": self.origin,
            "purchased_at": self.purchased_at,
            "plan_id": self.plan_id,
            "pantry_grams_before": canonical_grams(self.pantry_grams_before),
            "voided_at": self.voided_at,
            "grams_source": self.grams_source,
            "source_line_index": self.source_line_index,
            "segment_index": self.segment_index,
            "currency": self.currency,
            "basket_item_id": self.basket_item_id,
            "package_id": self.package_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PurchaseRecord":
        def opt_str(key: str) -> str | None:
            value = data.get(key)
            return str(value) if value is not None else None

        def opt_money(key: str, *, positive: bool = False) -> float | None:
            value = data.get(key)
            return normalize_money(value, positive=positive) if value is not None else None

        price_source = str(data.get("price_source", PRICE_SOURCE_UNKNOWN))
        origin = str(data.get("origin", ORIGIN_DIRECT_BUTTON))
        grams_source = str(data.get("grams_source", GRAMS_SOURCE_USER_ENTERED))
        if (
            price_source not in PRICE_SOURCES
            or origin not in ORIGINS
            or grams_source not in GRAMS_SOURCES
        ):
            raise ValueError(
                f"unknown price_source/origin/grams_source: "
                f"{price_source}/{origin}/{grams_source}"
            )
        grams = normalize_grams(data["grams"], positive=True)
        quantity = normalize_quantity(data.get("quantity", 1))
        line_total = opt_money("line_total", positive=True)
        source_line_index = (
            int(data["source_line_index"])
            if data.get("source_line_index") is not None else None
        )
        segment_index = (
            int(data["segment_index"])
            if data.get("segment_index") is not None else None
        )
        if grams <= 0 or quantity <= 0:
            raise ValueError("purchase grams and quantity must be positive")
        if line_total is not None and line_total <= 0:
            raise ValueError("confirmed item total must be positive")
        if source_line_index is not None and source_line_index < 0:
            raise ValueError("source line index must be non-negative")
        if segment_index is not None and segment_index < 0:
            raise ValueError("segment index must be non-negative")
        photo_path = opt_str("photo_path")
        if photo_path is not None:
            normalized_path = photo_path.replace("\\", "/")
            if (
                normalized_path.startswith("/")
                or (len(normalized_path) > 1 and normalized_path[1] == ":")
                or ".." in normalized_path.split("/")
            ):
                raise ValueError("purchase photo path must be relative")
            photo_path = normalized_path
        return cls(
            event_id=str(data["event_id"]),
            food_id=str(data["food_id"]),
            raw_name=str(data.get("raw_name", "")),
            brand=opt_str("brand"),
            package_label=opt_str("package_label"),
            grams=grams,
            quantity=quantity,
            line_total=line_total,
            estimated_line_cost=opt_money("estimated_line_cost"),
            price_source=price_source,
            store=str(data.get("store", "")),
            photo_path=photo_path,
            group_id=str(data.get("group_id", "")),
            origin=origin,
            purchased_at=str(data.get("purchased_at", "")),
            plan_id=opt_str("plan_id"),
            pantry_grams_before=normalize_grams(
                data.get("pantry_grams_before", 0.0)
            ),
            voided_at=opt_str("voided_at"),
            grams_source=grams_source,
            source_line_index=source_line_index,
            segment_index=segment_index,
            currency=opt_str("currency"),
            basket_item_id=opt_str("basket_item_id"),
            package_id=opt_str("package_id"),
        )
