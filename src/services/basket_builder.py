"""Build the shopping basket from a recipe plan's ingredient demand.

Recipe-first inverts the old flow: the meal plan fixes what must be cooked, so
the basket is simply (ingredient demand - pantry) rounded up into whole
packages and priced. Returns an OptimizationResult so the explanation service,
shopping list, and unused-food logic keep working unchanged.

One per-food pricing core feeds both ``price_demand`` (full result) and
``price_slice`` (cost + unpriced gap only, for the budget-repair inner loop),
so the two can never drift. Both are pure: no argument is mutated and results
are call-order independent. Each food is priced independently — its package
count depends only on its own total demand and pantry stock — so pricing a
slice of changed foods against cumulative demand reconstructs exact totals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

from models.basket import BasketItem, BudgetStatus, OptimizationResult, PantryUse
from models.food import Food, FoodGroup, Nutrients, PackageOption
from models.pricing import PriceQuote
from models.profile import HouseholdProfile
from services.nutrition import NutritionService

GRAM_EPSILON = 0.05
COST_EPSILON = 0.01  # dollars — totals within a cent count as equal
_DEEP_SHORTFALL = 0.5  # a nutrient below 50% of target => nutrition infeasible


def _cheapest_package(food: Food) -> PackageOption:
    return min(food.package_options, key=lambda p: (food.seed_cost_per_100(p), p.grams, p.label))


@dataclass(frozen=True)
class _FoodPrice:
    """One food's share of a demand, priced (the shared per-food core)."""

    food: Food
    grams_needed: float
    from_pantry: float
    gap: float
    item: BasketItem | None  # None when nothing must be bought or it is unpriced
    unpriced: bool  # a gap exists but there is no quote for this food


@dataclass(frozen=True)
class PricedDemand:
    items: tuple[BasketItem, ...]
    pantry_used: tuple[PantryUse, ...]
    nutrient_totals: Nutrients
    group_grams: tuple[tuple[FoodGroup, float], ...]  # sorted by FoodGroup.value
    total_cost: float
    total_gap_grams: float
    unpriced_gap_grams: float
    unpriced_food_ids: frozenset[str]


@dataclass(frozen=True)
class SlicePrice:
    total_cost: float
    unpriced_gap_grams: float
    unpriced_food_ids: frozenset[str]


def _iter_food_prices(
    demand: dict[str, float],
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: dict[str, PriceQuote] | None,
) -> Iterator[_FoodPrice]:
    quotes = quotes or {}
    for food_id in sorted(demand):
        grams_needed = demand[food_id]
        if grams_needed <= GRAM_EPSILON:
            continue
        food = foods_by_id.get(food_id)
        if food is None:
            continue
        available = pantry_items.get(food_id, 0.0)
        from_pantry = min(grams_needed, available)
        gap = grams_needed - from_pantry
        item: BasketItem | None = None
        unpriced = False
        if gap > GRAM_EPSILON:
            quote = quotes.get(food_id)
            if quote is None:
                unpriced = True  # cannot price it; count the gap, never guess
            else:
                pkg = _cheapest_package(food)
                count = max(1, math.ceil((gap - GRAM_EPSILON) / pkg.grams))
                item = BasketItem(food=food, package=pkg, count=count, quote=quote)
        yield _FoodPrice(food, grams_needed, from_pantry, gap, item, unpriced)


def price_demand(
    demand: dict[str, float],
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: dict[str, PriceQuote] | None,
) -> PricedDemand:
    items: list[BasketItem] = []
    pantry_used: list[PantryUse] = []
    nutrient_totals = Nutrients()
    group_grams: dict[FoodGroup, float] = {}
    total_gap = 0.0
    unpriced_gap = 0.0
    unpriced_ids: set[str] = set()

    for fp in _iter_food_prices(demand, pantry_items, foods_by_id, quotes):
        # Nutrition of the whole demand (what the plan actually consumes).
        nutrient_totals = nutrient_totals.plus(
            fp.food.nutrients_per_purchased_100g().scaled(fp.grams_needed / 100.0))
        group_grams[fp.food.food_group] = group_grams.get(fp.food.food_group, 0.0) + fp.grams_needed
        if fp.from_pantry > GRAM_EPSILON:
            pantry_used.append(PantryUse(food=fp.food, grams=round(fp.from_pantry, 3)))
        if fp.gap > GRAM_EPSILON:
            total_gap += fp.gap
            if fp.unpriced:
                unpriced_gap += fp.gap
                unpriced_ids.add(fp.food.id)
            else:
                items.append(fp.item)

    return PricedDemand(
        items=tuple(items),
        pantry_used=tuple(pantry_used),
        nutrient_totals=nutrient_totals,
        group_grams=tuple(sorted(group_grams.items(), key=lambda kv: kv[0].value)),
        total_cost=round(sum(item.cost for item in items), 2),
        total_gap_grams=total_gap,
        unpriced_gap_grams=unpriced_gap,
        unpriced_food_ids=frozenset(unpriced_ids),
    )


def price_slice(
    demand: dict[str, float],
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: dict[str, PriceQuote] | None,
) -> SlicePrice:
    """Same per-food core as ``price_demand`` but skips the nutrient/group/
    pantry-use aggregation the repair inner loop does not need."""
    items: list[BasketItem] = []
    unpriced_gap = 0.0
    unpriced_ids: set[str] = set()
    for fp in _iter_food_prices(demand, pantry_items, foods_by_id, quotes):
        if fp.gap > GRAM_EPSILON:
            if fp.unpriced:
                unpriced_gap += fp.gap
                unpriced_ids.add(fp.food.id)
            else:
                items.append(fp.item)
    return SlicePrice(
        total_cost=round(sum(item.cost for item in items), 2),
        unpriced_gap_grams=unpriced_gap,
        unpriced_food_ids=frozenset(unpriced_ids),
    )


def build_shopping_result(
    demand: dict[str, float],
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: dict[str, PriceQuote],
    profile: HouseholdProfile,
    nutrition: NutritionService,
    budget: float,
    horizon_days: int,
    excluded: dict[str, str] | None = None,
) -> OptimizationResult:
    excluded = excluded or {}
    priced = price_demand(demand, pantry_items, foods_by_id, quotes)

    targets = nutrition.household_targets(profile, horizon_days)
    gaps = tuple(NutritionService.gaps(priced.nutrient_totals, targets))
    nutrition_feasible = all(
        (g.achieved / g.target) >= _DEEP_SHORTFALL for g in gaps if g.target > 0)

    # Priority order: a known overage always wins — missing prices can only
    # push the real total further up, never rescue it.
    if priced.total_cost > budget + COST_EPSILON:
        budget_status = BudgetStatus.OVER
    elif priced.unpriced_gap_grams > GRAM_EPSILON:
        budget_status = BudgetStatus.UNKNOWN
    else:
        budget_status = BudgetStatus.WITHIN

    relaxed: list[str] = []
    if budget_status is BudgetStatus.OVER and priced.items:
        relaxed.append(
            f"Estimated basket ${priced.total_cost:.2f} exceeds the ${budget:.2f} budget; "
            f"reduce the plan, raise the budget, or check for cheaper stores.")
    unpriced_names: tuple[str, ...] = ()
    if priced.unpriced_gap_grams > GRAM_EPSILON:
        # Name the foods, never a weight percentage — one unpriced ingredient
        # can dominate the real cost regardless of its grams.
        names = sorted(foods_by_id[fid].name for fid in priced.unpriced_food_ids)
        unpriced_names = tuple(names)
        shown = ", ".join(names[:3]) + (f", and {len(names) - 3} more" if len(names) > 3 else "")
        relaxed.append(f"No price data for: {shown} — the total above may be understated.")

    return OptimizationResult(
        items=priced.items,
        total_cost=priced.total_cost,
        budget=budget,
        score=0.0,
        nutrient_totals=priced.nutrient_totals,
        gaps=gaps,
        group_coverage=dict(priced.group_grams),
        groups_covered=len(priced.group_grams),
        distinct_foods=len(priced.items),
        budget_status=budget_status,
        nutrition_feasible=nutrition_feasible,
        relaxed_constraints=tuple(relaxed),
        dominance_flags=(),
        unpriced_food_names=unpriced_names,
        excluded_foods=dict(excluded),
        horizon_days=horizon_days,
        pantry_used=priced.pantry_used,
    )
