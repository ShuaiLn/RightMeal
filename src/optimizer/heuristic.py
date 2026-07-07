"""Greedy + local-search basket optimizer.

Deterministic by construction: canonical sort orders, no randomness, no
wall-clock cutoffs. The budget is a hard constraint; food-group coverage,
variety, and dominance are soft constraints enforced when feasible and
reported honestly when not.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from models.basket import BasketItem, OptimizationResult
from models.food import Food, FoodGroup, Nutrients
from models.pricing import PriceQuote, PriceSource
from models.profile import HouseholdProfile
from optimizer.config import OptimizerConfig
from optimizer.filters import apply_exclusions
from optimizer.scoring import BasketState, PurchaseUnit, penalty_breakdown, score_basket
from services.nutrition import NutritionService

EPSILON = 1e-9


def unit_cost_for(food: Food, package, quote: PriceQuote) -> float:
    """Cost of one package under a quote (mirrors BasketItem.cost)."""
    return BasketItem(food=food, package=package, count=1, quote=quote).cost


def optimize(
    foods: Sequence[Food],
    quotes: Mapping[str, PriceQuote],
    profile: HouseholdProfile,
    budget: float,
    horizon_days: int,
    nutrition: NutritionService,
    config: OptimizerConfig = OptimizerConfig(),
) -> OptimizationResult:
    allowed, excluded = apply_exclusions(foods, profile)
    targets = nutrition.household_targets(profile, horizon_days)
    group_caps = nutrition.group_caps_g(profile, horizon_days)

    horizon_factor = horizon_days / 7.0
    members = max(profile.total_members, 1)
    food_caps = {
        food.id: max(food.max_weekly_grams * members * horizon_factor, food.smallest_package.grams)
        for food in allowed
    }

    units = sorted(
        (
            PurchaseUnit(
                food=food,
                package=pkg,
                unit_cost=unit_cost_for(food, pkg, quotes[food.id]),
                confidence=quotes[food.id].confidence,
            )
            for food in allowed
            for pkg in food.package_options
        ),
        key=lambda u: u.key,
    )
    units_by_key = {u.key: u for u in units}

    available_groups = {food.food_group for food in allowed}
    group_target = min(config.group_target, len(available_groups))
    min_distinct = min(config.min_distinct(profile.total_members), len(allowed))

    state = BasketState()

    def can_add(unit: PurchaseUnit) -> bool:
        if state.total_cost + unit.unit_cost > budget + EPSILON:
            return False
        food = unit.food
        if state.food_grams.get(food.id, 0.0) + unit.grams > food_caps[food.id] + EPSILON:
            return False
        group_cap = group_caps[food.food_group]
        return state.group_grams.get(food.food_group, 0.0) + unit.grams <= group_cap + EPSILON

    def full_score() -> float:
        return score_basket(state, targets, config, min_distinct, group_target)

    def core_score() -> float:
        return score_basket(
            state, targets, config, min_distinct, group_target, include_dominance=False
        )

    def greedy_fill(use_full_score: bool) -> None:
        score_fn = full_score if use_full_score else core_score
        while True:
            current = score_fn()
            best = None  # (delta_per_dollar, -unit_cost) maximized; ties -> key order
            for unit in units:
                if not can_add(unit):
                    continue
                state.add(unit)
                delta = score_fn() - current
                state.remove(unit)
                if delta <= EPSILON:
                    continue
                rank = (delta / unit.unit_cost if unit.unit_cost > 0 else float("inf"), -unit.unit_cost)
                if best is None or rank > best[0]:
                    best = (rank, unit)
            if best is None:
                return
            state.add(best[1])

    # 1. Greedy growth on the core score (dominance excluded — early baskets
    #    always exceed the 35% share and would stall at empty otherwise).
    greedy_fill(use_full_score=False)

    # 2. Coverage repair: buy into missing food groups while affordable.
    while state.groups_covered < group_target:
        missing = available_groups - set(state.group_grams)
        candidates = sorted(
            (u for u in units if u.food.food_group in missing and can_add(u)),
            key=lambda u: (u.unit_cost, u.key),
        )
        if not candidates:
            break
        state.add(candidates[0])

    # 3. Bounded local search on the full score: best swap per sweep, then
    #    cost-saving removals, then a full-score greedy top-up.
    for _ in range(config.max_sweeps):
        improved = False

        current_score = full_score()
        best_swap = None  # ((score, -cost), remove_key, add_key)
        for remove_key in sorted(state.counts):
            unit_out = units_by_key[remove_key]
            state.remove(unit_out)
            for unit_in in units:
                if unit_in.key == remove_key or not can_add(unit_in):
                    continue
                state.add(unit_in)
                rank = (full_score(), -state.total_cost)
                if best_swap is None or rank > best_swap[0]:
                    best_swap = (rank, remove_key, unit_in.key)
                state.remove(unit_in)
            state.add(unit_out)

        if best_swap is not None:
            (swap_score, neg_cost), remove_key, add_key = best_swap
            better_score = swap_score > current_score + EPSILON
            same_score_cheaper = (
                swap_score >= current_score - EPSILON and -neg_cost < state.total_cost - EPSILON
            )
            if better_score or same_score_cheaper:
                state.remove(units_by_key[remove_key])
                state.add(units_by_key[add_key])
                improved = True

        removed_any = True
        while removed_any:
            removed_any = False
            baseline = full_score()
            for key in sorted(state.counts):
                unit = units_by_key[key]
                state.remove(unit)
                if full_score() >= baseline - EPSILON:
                    baseline = full_score()
                    improved = removed_any = True
                    break
                state.add(unit)

        before_topup = full_score()
        greedy_fill(use_full_score=True)
        if full_score() > before_topup + EPSILON:
            improved = True

        if not improved:
            break

    # 4. Assemble the verified result.
    items = tuple(
        BasketItem(
            food=units_by_key[key].food,
            package=units_by_key[key].package,
            count=count,
            quote=quotes[key[0]],
        )
        for key, count in sorted(state.counts.items())
    )
    total_cost = sum(item.cost for item in items)
    nutrient_totals = Nutrients.from_dict(state.nutrient_totals)
    gaps = tuple(nutrition.gaps(nutrient_totals, targets))
    penalties = {
        name: round(value, 4)
        for name, value in penalty_breakdown(
            state, targets, config, min_distinct, group_target
        ).items()
        if value > EPSILON
    }

    dominance_flags = _dominance_flags(state, config)
    ratios = {
        name: (nutrient_totals.get(name) / targets.get(name) if targets.get(name) > 0 else 1.0)
        for name in Nutrients.NAMES
    }
    deep_shortfalls = sorted(
        Nutrients.NUTRIENT_LABELS[name]
        for name, ratio in ratios.items()
        if ratio < config.missing_floor
    )

    budget_feasible = not state.is_empty
    nutrition_feasible = (
        budget_feasible
        and state.groups_covered >= group_target
        and state.distinct_foods >= min_distinct
        and not dominance_flags
        and not deep_shortfalls
    )

    relaxed: list[str] = []
    if not budget_feasible:
        relaxed.append(f"The budget of ${budget:.2f} is too low to buy any food package.")
    else:
        if state.groups_covered < group_target:
            relaxed.append(
                f"Only {state.groups_covered} of 6 food groups fit the budget "
                f"(target: at least {group_target})."
            )
        if state.distinct_foods < min_distinct:
            relaxed.append(
                f"Only {state.distinct_foods} distinct foods fit the budget "
                f"(target: at least {min_distinct})."
            )
        if dominance_flags:
            relaxed.append("Some items exceed 35% of the basket's calories or cost (flagged).")
        if deep_shortfalls:
            relaxed.append(
                "Deep shortfalls (under 50% of target): " + ", ".join(deep_shortfalls) + "."
            )

    return OptimizationResult(
        items=items,
        total_cost=round(total_cost, 2),
        budget=budget,
        score=round(full_score(), 4),
        nutrient_totals=nutrient_totals,
        gaps=gaps,
        group_coverage=dict(sorted(state.group_grams.items(), key=lambda kv: kv[0].value)),
        groups_covered=state.groups_covered,
        distinct_foods=state.distinct_foods,
        budget_feasible=budget_feasible,
        nutrition_feasible=nutrition_feasible,
        relaxed_constraints=tuple(relaxed),
        dominance_flags=dominance_flags,
        excluded_foods=excluded,
        penalties_applied=penalties,
        horizon_days=horizon_days,
    )


def _dominance_flags(state: BasketState, config: OptimizerConfig) -> tuple[str, ...]:
    flags: list[str] = []
    total_cal = sum(state.food_calories.values())
    for food_id in sorted(state.food_grams):
        if total_cal > 0:
            cal_share = state.food_calories[food_id] / total_cal
            if cal_share > config.dominance_share + EPSILON:
                flags.append(f"{food_id} provides {cal_share:.0%} of total calories")
        if state.total_cost > 0:
            cost_share = state.food_cost[food_id] / state.total_cost
            if cost_share > config.dominance_share + EPSILON:
                flags.append(f"{food_id} takes {cost_share:.0%} of total cost")
    return tuple(flags)
