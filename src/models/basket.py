"""Basket and optimization result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from models.food import Food, FoodGroup, Nutrients, PackageOption
from models.pricing import (
    PackageOffer,
    PriceQuote,
    PriceSource,
    dollars_to_cents,
    package_id_for,
)


class BudgetStatus(str, Enum):
    """Whether the current estimated basket fits the estimated cap.

    UNKNOWN means price data is missing for part of the demand — the known
    total may understate the real cost, so neither WITHIN nor OVER can be
    claimed. Readers must branch on all three states explicitly; there is
    deliberately no boolean view of this (``UNKNOWN`` is not falsy-OVER).
    """

    WITHIN = "within"
    OVER = "over"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BasketItem:
    """A number of whole packages of one food in the planned basket."""

    food: Food
    package: PackageOption
    count: int
    quote: PriceQuote
    offer: PackageOffer | None = None

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("basket item count must be positive")
        if self.offer is not None:
            if self.offer.food_id != self.food.id:
                raise ValueError("package offer belongs to a different food")
            if self.offer.package_id != package_id_for(self.food.id, self.package):
                raise ValueError("package offer belongs to a different package")

    @property
    def grams(self) -> float:
        return self.package.grams * self.count

    @property
    def cost(self) -> float:
        """Estimated cost: normalized provider price applied to package size.

        MVP simplification — real stores price package sizes non-linearly,
        so seed quotes keep per-package granularity while live quotes are
        applied per 100 g / 100 ml.
        """
        if self.offer is not None:
            return self.offer.price_cents * self.count / 100.0
        if self.quote.source is PriceSource.SEED_ESTIMATE:
            return self.package.seed_price * self.count
        if self.food.is_liquid and self.quote.normalized_unit == "100ml":
            assert self.package.ml is not None
            return self.quote.normalized_unit_price * (self.package.ml / 100.0) * self.count
        return self.quote.normalized_unit_price * (self.package.grams / 100.0) * self.count

    @property
    def package_id(self) -> str:
        return self.offer.package_id if self.offer is not None else package_id_for(
            self.food.id, self.package
        )

    @property
    def offer_id(self) -> str:
        if self.offer is not None:
            return self.offer.offer_id
        return PackageOffer.from_quote(self.food, self.quote).offer_id

    @property
    def unit_cost_cents(self) -> int:
        if self.offer is not None:
            return self.offer.price_cents
        return dollars_to_cents(self.cost / self.count)

    @property
    def total_cost_cents(self) -> int:
        if self.offer is not None:
            return self.offer.price_cents * self.count
        return dollars_to_cents(self.cost)

    @property
    def nutrients(self) -> Nutrients:
        """Total nutrients contributed by this item (edible-fraction adjusted)."""
        return self.food.nutrients_per_purchased_100g().scaled(self.grams / 100.0)

    @property
    def quantity_label(self) -> str:
        return f"{self.count} × {self.package.label}"


@dataclass(frozen=True)
class PantryUse:
    """Grams of one already-owned food the optimizer counts on using (cost 0)."""

    food: Food
    grams: float


@dataclass(frozen=True)
class NutrientGap:
    nutrient: str  # Nutrients field name
    achieved: float
    target: float

    @property
    def pct(self) -> float:
        if self.target <= 0:
            return 100.0
        return 100.0 * self.achieved / self.target


@dataclass(frozen=True)
class OptimizationResult:
    """The verified output of the basket optimizer.

    ``total_cost`` is an *estimated planning total* assembled from mixed
    price sources — never a single store's real checkout total.
    """

    items: tuple[BasketItem, ...]
    total_cost: float
    budget: float
    score: float
    nutrient_totals: Nutrients
    gaps: tuple[NutrientGap, ...]  # nutrients below 100% of target
    group_coverage: dict[FoodGroup, float]  # grams per group (covered groups only)
    groups_covered: int
    distinct_foods: int
    budget_status: BudgetStatus  # known-over always wins over unknown
    nutrition_feasible: bool  # soft nutrition constraints all met
    relaxed_constraints: tuple[str, ...]
    dominance_flags: tuple[str, ...]
    # Sorted display names of demanded foods that had no price quote (for the
    # explanation payload; the programmatic ids live on PricedDemand).
    unpriced_food_names: tuple[str, ...] = ()
    excluded_foods: dict[str, str] = field(default_factory=dict)  # food_id -> reason
    penalties_applied: dict[str, float] = field(default_factory=dict)
    horizon_days: int = 7
    pantry_used: tuple[PantryUse, ...] = ()  # pantry supply seeded into this plan
    # Deferred local-price fallback provenance.  The ids are stable catalog ids;
    # source_mix remains the complete per-line view.
    local_fallback_used: bool = False
    local_fallback_food_ids: tuple[str, ...] = ()
    local_fallback_sources: tuple[PriceSource, ...] = ()
    unpriced_food_ids: tuple[str, ...] = ()

    @property
    def source_mix(self) -> dict[PriceSource, int]:
        mix: dict[PriceSource, int] = {}
        for item in self.items:
            # Native package-offer provenance is authoritative.  ``quote`` is
            # retained as a presentation/legacy adapter and may be reconstructed.
            source = item.offer.source if item.offer is not None else item.quote.source
            mix[source] = mix.get(source, 0) + 1
        return mix

    @property
    def total_cost_cents(self) -> int:
        return sum(item.total_cost_cents for item in self.items)

    @property
    def local_fallback_food_count(self) -> int:
        return len(self.local_fallback_food_ids)
