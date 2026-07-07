"""Basket scoring: nutrition adequacy minus soft-constraint penalties.

Cost is deliberately NOT part of the score — the budget is a hard constraint
and cost only breaks ties (the secondary objective is the cheaper basket among
similar-adequacy baskets).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models.food import Food, FoodGroup, Nutrients, PackageOption
from optimizer.config import OptimizerConfig


@dataclass(frozen=True)
class PurchaseUnit:
    """One whole package of one food — the optimizer's atomic decision."""

    food: Food
    package: PackageOption
    unit_cost: float
    confidence: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.food.id, self.package.label)

    @property
    def grams(self) -> float:
        return self.package.grams


@dataclass
class BasketState:
    """Mutable aggregate totals for fast add/remove/score cycles."""

    counts: dict[tuple[str, str], int] = field(default_factory=dict)
    total_cost: float = 0.0
    nutrient_totals: dict[str, float] = field(
        default_factory=lambda: {n: 0.0 for n in Nutrients.NAMES}
    )
    food_grams: dict[str, float] = field(default_factory=dict)
    food_cost: dict[str, float] = field(default_factory=dict)
    food_calories: dict[str, float] = field(default_factory=dict)
    food_confidence: dict[str, float] = field(default_factory=dict)
    group_grams: dict[FoodGroup, float] = field(default_factory=dict)

    def add(self, unit: PurchaseUnit, sign: int = 1) -> None:
        food = unit.food
        self.counts[unit.key] = self.counts.get(unit.key, 0) + sign
        if self.counts[unit.key] <= 0:
            del self.counts[unit.key]
        self.total_cost += sign * unit.unit_cost
        per_purchased_100g = food.nutrients_per_purchased_100g()
        factor = sign * unit.grams / 100.0
        for name in Nutrients.NAMES:
            self.nutrient_totals[name] += per_purchased_100g.get(name) * factor
        self.food_grams[food.id] = self.food_grams.get(food.id, 0.0) + sign * unit.grams
        self.food_cost[food.id] = self.food_cost.get(food.id, 0.0) + sign * unit.unit_cost
        self.food_calories[food.id] = (
            self.food_calories.get(food.id, 0.0)
            + per_purchased_100g.calories_kcal * factor
        )
        self.food_confidence[food.id] = unit.confidence
        self.group_grams[food.food_group] = (
            self.group_grams.get(food.food_group, 0.0) + sign * unit.grams
        )
        if self.food_grams[food.id] <= 1e-9:
            self.food_grams.pop(food.id)
            self.food_cost.pop(food.id, None)
            self.food_calories.pop(food.id, None)
            self.food_confidence.pop(food.id, None)
        if self.group_grams[food.food_group] <= 1e-9:
            self.group_grams.pop(food.food_group)

    def remove(self, unit: PurchaseUnit) -> None:
        self.add(unit, sign=-1)

    @property
    def distinct_foods(self) -> int:
        return len(self.food_grams)

    @property
    def groups_covered(self) -> int:
        return len(self.group_grams)

    @property
    def is_empty(self) -> bool:
        return not self.counts


def nutrient_ratios(state: BasketState, targets: Nutrients) -> dict[str, float]:
    ratios: dict[str, float] = {}
    for name in Nutrients.NAMES:
        target = targets.get(name)
        ratios[name] = state.nutrient_totals[name] / target if target > 0 else 1.0
    return ratios


def penalty_breakdown(
    state: BasketState,
    targets: Nutrients,
    config: OptimizerConfig,
    min_distinct: int,
    group_target: int,
) -> dict[str, float]:
    """All penalty terms of the full score, by name."""
    ratios = nutrient_ratios(state, targets)

    missing = config.penalty_missing * sum(
        max(0.0, config.missing_floor - r) for r in ratios.values()
    )
    groups = config.penalty_groups * max(0, group_target - state.groups_covered)
    variety = config.penalty_variety * max(0, min_distinct - state.distinct_foods)

    dominance = 0.0
    if state.total_cost > 0:
        total_cal = sum(state.food_calories.values())
        for food_id in state.food_grams:
            cost_share = state.food_cost[food_id] / state.total_cost
            dominance += config.penalty_dominance * max(0.0, cost_share - config.dominance_share)
            if total_cal > 0:
                cal_share = state.food_calories[food_id] / total_cal
                dominance += config.penalty_dominance * max(
                    0.0, cal_share - config.dominance_share
                )

    low_confidence = 0.0
    if state.total_cost > 0:
        low_confidence = config.penalty_low_confidence * sum(
            (state.food_cost[fid] / state.total_cost) * (1.0 - state.food_confidence[fid])
            for fid in state.food_cost
        )

    overshoot = config.penalty_overshoot * max(
        0.0, ratios["calories_kcal"] - config.overshoot_ratio
    )

    return {
        "missing_nutrients": missing,
        "food_group_diversity": groups,
        "variety": variety,
        "dominance": dominance,
        "low_confidence_prices": low_confidence,
        "calorie_overshoot": overshoot,
    }


def adequacy_score(state: BasketState, targets: Nutrients, config: OptimizerConfig) -> float:
    """Weighted nutrition adequacy, normalized to a maximum of 100 points."""
    ratios = nutrient_ratios(state, targets)
    weights = {
        name: (
            config.weight_calories
            if name == "calories_kcal"
            else config.weight_protein if name == "protein_g" else config.weight_other
        )
        for name in Nutrients.NAMES
    }
    total_weight = sum(weights.values())
    return 100.0 * sum(w * min(ratios[n], 1.0) for n, w in weights.items()) / total_weight


def score_basket(
    state: BasketState,
    targets: Nutrients,
    config: OptimizerConfig,
    min_distinct: int,
    group_target: int,
    include_dominance: bool = True,
) -> float:
    """Full basket score (or the core score without the dominance term).

    The dominance penalty is excluded during greedy growth — the first items
    in any basket inevitably exceed the 35% share — and enforced during local
    search and in the final reported score.
    """
    penalties = penalty_breakdown(state, targets, config, min_distinct, group_target)
    if not include_dominance:
        penalties = {k: v for k, v in penalties.items() if k != "dominance"}
    return adequacy_score(state, targets, config) - sum(penalties.values())
