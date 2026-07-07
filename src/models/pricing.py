"""Pricing models: sources, quotes, and locations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PriceSource(str, Enum):
    """Where a price quote came from, in fallback priority order."""

    KROGER_REAL_PRICE = "kroger_real_price"
    INSTACART_NUMERIC_PRICE = "instacart_numeric_price"
    BLS_REGIONAL_AVERAGE = "bls_regional_average"
    SEED_ESTIMATE = "seed_estimate"


PRICE_SOURCE_LABELS: dict[PriceSource, str] = {
    PriceSource.KROGER_REAL_PRICE: "Kroger/Ralphs real price",
    PriceSource.INSTACART_NUMERIC_PRICE: "Instacart numeric product price",
    PriceSource.BLS_REGIONAL_AVERAGE: "BLS regional average estimate",
    PriceSource.SEED_ESTIMATE: "Seed estimate",
}


@dataclass(frozen=True)
class Location:
    city: str
    zip_code: str


@dataclass(frozen=True)
class PriceQuote:
    """A normalized price for one food from one provider.

    ``normalized_unit_price`` is cost per 100 g for solid foods and cost per
    100 ml for liquids; ``normalized_unit`` says which basis applies.
    """

    food_name: str
    matched_product_name: str
    price: float
    unit: str
    unit_price: float
    normalized_unit_price: float
    raw_unit: str
    normalized_unit: str  # "100g" | "100ml"
    store: str
    source: PriceSource
    confidence: float
    is_estimate: bool
    last_updated: str  # ISO 8601
    match_reason: str
    provider_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, PriceSource):
            raise ValueError(f"source must be a PriceSource, got {self.source!r}")
        if self.normalized_unit not in ("100g", "100ml"):
            raise ValueError(f"normalized_unit must be '100g' or '100ml', got {self.normalized_unit!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0, 1], got {self.confidence}")
        if self.price < 0 or self.unit_price < 0 or self.normalized_unit_price < 0:
            raise ValueError("prices must be non-negative")
