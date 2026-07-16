"""Construction and validation of explicitly incomplete food-coverage plans.

A partial plan is never a smaller household or a shorter calendar. It keeps
every member, day, and meal slot, and scales all three meals of each day by the
same one-percent-precision factor. Calories and protein must each reach at least
60% of that household's daily target; allergies and diet rules have already
been applied by the standard scheduler and are never relaxed here.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

from models.food import Nutrients
from models.meals import DayPlan, Meal, MealPlan, MealPortion, SLOT_ORDER
from models.profile import HouseholdProfile
from models.plan import DailyFoodCoverage
from services.nutrition import NutritionService

MIN_FOOD_COVERAGE = 0.60
PARTIAL_PLAN_WARNING = (
    "This is not a complete food plan. Additional food is required."
)


class PartialPlanError(ValueError):
    pass


def _day_nutrients(day: DayPlan) -> Nutrients:
    total = Nutrients()
    for meal in day.meals:
        total = total.plus(meal.nutrients)
    return total


def _ceil_percent(value: float) -> float:
    return min(1.0, math.ceil(max(0.0, value) * 100.0 - 1e-9) / 100.0)


def required_daily_portion_scale(
    day: DayPlan,
    daily_targets: Nutrients,
    minimum: float = MIN_FOOD_COVERAGE,
) -> float:
    """Smallest 1%-precision common scale meeting kcal and protein floors."""

    totals = _day_nutrients(day)
    requirements = [minimum]
    for achieved, target in (
        (totals.calories_kcal, daily_targets.calories_kcal),
        (totals.protein_g, daily_targets.protein_g),
    ):
        if target <= 0:
            continue
        if achieved <= 0:
            raise PartialPlanError("A day has no calories or protein to scale.")
        requirements.append((minimum * target) / achieved)
    scale = _ceil_percent(max(requirements))
    if scale > 1.0 or any(value > 1.0 + 1e-9 for value in requirements):
        raise PartialPlanError(
            "The complete candidate does not reach the partial daily floor."
        )
    return scale


def _scale_portion(portion: MealPortion, scale: float) -> MealPortion:
    return replace(
        portion,
        grams=portion.grams * scale,
        cooked_grams=(
            portion.cooked_grams * scale
            if portion.cooked_grams is not None else None
        ),
    )


def _scale_meal(meal: Meal, members: int, scale: float) -> Meal:
    equivalents = members * scale
    return replace(
        meal,
        portions=tuple(_scale_portion(portion, scale) for portion in meal.portions),
        # Legacy wire alias; explicit fields below are authoritative.
        servings=equivalents,
        side_servings=meal.side_servings * scale,
        household_member_count=members,
        full_serving_equivalent=equivalents,
        portion_scale=scale,
    )


def build_partial_food_coverage_plan(
    complete_candidate: MealPlan,
    profile: HouseholdProfile,
    nutrition: NutritionService,
    *,
    minimum: float = MIN_FOOD_COVERAGE,
) -> tuple[MealPlan, tuple[DailyFoodCoverage, ...]]:
    """Scale a complete candidate without changing people, slots, or dates."""

    daily_targets = nutrition.household_daily_targets(profile)
    scales = tuple(
        required_daily_portion_scale(day, daily_targets, minimum)
        for day in complete_candidate.days
    )
    return scale_partial_food_coverage_plan(
        complete_candidate,
        profile,
        nutrition,
        scales,
        minimum=minimum,
    )


def scale_partial_food_coverage_plan(
    complete_candidate: MealPlan,
    profile: HouseholdProfile,
    nutrition: NutritionService,
    daily_portion_scales: Sequence[float],
    *,
    minimum: float = MIN_FOOD_COVERAGE,
) -> tuple[MealPlan, tuple[DailyFoodCoverage, ...]]:
    """Build one exactly-scaled partial candidate and its daily evidence.

    The caller supplies one-percent portion scales.  This function deliberately
    rebuilds every meal and every nutrition ratio instead of adjusting prior
    evidence arithmetically, so it is safe to use for the package-aware partial
    optimizer's repeated trial candidates.
    """

    if profile.total_members <= 0:
        raise PartialPlanError("A partial plan needs at least one household member.")
    if len(complete_candidate.days) != complete_candidate.horizon_days:
        raise PartialPlanError("A partial plan must keep the full date horizon.")
    if len(daily_portion_scales) != complete_candidate.horizon_days:
        raise PartialPlanError("A portion scale is required for every plan day.")
    daily_targets = nutrition.household_daily_targets(profile)
    days: list[DayPlan] = []
    coverage: list[DailyFoodCoverage] = []
    for expected_index, (day, requested_scale) in enumerate(
        zip(complete_candidate.days, daily_portion_scales)
    ):
        if day.day_index != expected_index:
            raise PartialPlanError("Partial-plan days must be contiguous.")
        if tuple(meal.slot for meal in day.meals) != SLOT_ORDER:
            raise PartialPlanError("Every partial-plan day must keep all three meals.")
        requested = float(requested_scale)
        if not math.isclose(
            requested * 100.0,
            round(requested * 100.0),
            abs_tol=1e-7,
        ):
            raise PartialPlanError("Partial-plan portions must use one-percent steps.")
        scale = round(requested, 2)
        if not 0 < scale <= 1.0:
            raise PartialPlanError("Partial-plan portion scales must be within (0, 1].")
        scaled = DayPlan(
            day_index=day.day_index,
            meals=tuple(
                _scale_meal(meal, profile.total_members, scale)
                for meal in day.meals
            ),
        )
        totals = _day_nutrients(scaled)
        calories_ratio = (
            totals.calories_kcal / daily_targets.calories_kcal
            if daily_targets.calories_kcal > 0 else 1.0
        )
        protein_ratio = (
            totals.protein_g / daily_targets.protein_g
            if daily_targets.protein_g > 0 else 1.0
        )
        if calories_ratio + 1e-9 < minimum or protein_ratio + 1e-9 < minimum:
            raise PartialPlanError("Partial-plan scaling missed its daily floor.")
        days.append(scaled)
        coverage.append(DailyFoodCoverage(
            day_index=day.day_index,
            portion_scale=scale,
            calories_ratio=calories_ratio,
            protein_ratio=protein_ratio,
        ))
    consumed = Nutrients()
    for day in days:
        consumed = consumed.plus(_day_nutrients(day))
    return (
        MealPlan(
            days=tuple(days),
            pantry_carryover=dict(complete_candidate.pantry_carryover),
            consumed_totals=consumed,
            horizon_days=complete_candidate.horizon_days,
        ),
        tuple(coverage),
    )


def rolling_recipe_limit_satisfied(
    plan: MealPlan,
    *,
    max_uses: int,
    window_days: int = 7,
) -> bool:
    """Check recipe repetition in every rolling window, deterministically."""

    if max_uses <= 0 or window_days <= 0:
        raise ValueError("recipe repeat limits must be positive")
    days = list(plan.days)
    for start in range(len(days)):
        counts: dict[str, int] = {}
        for day in days[start:start + window_days]:
            for meal in day.meals:
                for recipe_id in (meal.recipe_id, meal.side_recipe_id):
                    if recipe_id:
                        counts[recipe_id] = counts.get(recipe_id, 0) + 1
                        if counts[recipe_id] > max_uses:
                            return False
    return True


def partial_repeat_limit(plan: MealPlan) -> int | None:
    """Use the strict max-two rule first; max-three is the only relaxation."""

    if rolling_recipe_limit_satisfied(plan, max_uses=2):
        return 2
    if rolling_recipe_limit_satisfied(plan, max_uses=3):
        return 3
    return None


def validate_partial_food_coverage(
    plan: MealPlan,
    coverage: Sequence[DailyFoodCoverage],
    *,
    minimum: float = MIN_FOOD_COVERAGE,
) -> None:
    if len(plan.days) != plan.horizon_days or len(coverage) != plan.horizon_days:
        raise PartialPlanError("Partial coverage must exist for every plan day.")
    for day, daily in zip(plan.days, coverage):
        if len(day.meals) != 3:
            raise PartialPlanError("A partial plan must have three meals every day.")
        scales = {round(meal.portion_scale, 2) for meal in day.meals}
        if scales != {round(daily.portion_scale, 2)}:
            raise PartialPlanError("All meals in a day must use one portion scale.")
        if (
            daily.calories_ratio + 1e-9 < minimum
            or daily.protein_ratio + 1e-9 < minimum
        ):
            raise PartialPlanError("Daily calories and protein must meet the 60% floor.")


__all__ = [
    "DailyFoodCoverage", "MIN_FOOD_COVERAGE", "PARTIAL_PLAN_WARNING",
    "PartialPlanError", "build_partial_food_coverage_plan",
    "partial_repeat_limit", "required_daily_portion_scale",
    "rolling_recipe_limit_satisfied", "scale_partial_food_coverage_plan",
    "validate_partial_food_coverage",
]
