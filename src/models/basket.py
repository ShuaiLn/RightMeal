"""Basket and optimization result models."""

from __future__ import annotations

from dataclasses import dataclass, field

from models.food import Food, FoodGroup, Nutrients, PackageOption
from models.pricing import PriceQuote, PriceSource


@dataclass(frozen=True)
class BasketItem:
    """A number of whole packages of one food in the planned basket."""

    food: Food
    package: PackageOption
    count: int
    quote: PriceQuote

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
        if self.quote.source is PriceSource.SEED_ESTIMATE:
            return self.package.seed_price * self.count
        if self.food.is_liquid and self.quote.normalized_unit == "100ml":
            assert self.package.ml is not None
            return self.quote.normalized_unit_price * (self.package.ml / 100.0) * self.count
        return self.quote.normalized_unit_price * (self.package.grams / 100.0) * self.count

    @property
    def nutrients(self) -> Nutrients:
        """Total nutrients contributed by this item (edible-fraction adjusted)."""
        return self.food.nutrients_per_purchased_100g().scaled(self.grams / 100.0)

    @property
    def quantity_label(self) -> str:
        return f"{self.count} × {self.package.label}"


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
    budget_feasible: bool  # a non-empty basket fits within the budget
    nutrition_feasible: bool  # soft nutrition constraints all met
    relaxed_constraints: tuple[str, ...]
    dominance_flags: tuple[str, ...]
    excluded_foods: dict[str, str] = field(default_factory=dict)  # food_id -> reason
    penalties_applied: dict[str, float] = field(default_factory=dict)
    horizon_days: int = 7

    @property
    def source_mix(self) -> dict[PriceSource, int]:
        mix: dict[PriceSource, int] = {}
        for item in self.items:
            mix[item.quote.source] = mix.get(item.quote.source, 0) + 1
        return mix
