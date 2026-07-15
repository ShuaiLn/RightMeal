"""The recipe-first meal-plan orchestration.

There is a single planning path: real catalog recipes are filtered, scored, and
validated into a plan, then the shopping basket is built from the plan's
ingredient demand. (The retired template/optimizer engine has been removed.)
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from models.basket import BudgetStatus, OptimizationResult
from models.meals import MealPlan
from models.profile import HouseholdProfile
from models.recipe import Recipe
from planner.demand import ingredient_demand
from planner.recipe_scheduler import (
    PlanGenerationError, RecipePlanConfig, RepairStats, VarietyMode,
    _repair_budget, build_planner_context, build_recipe_plan, finalize_meal_plan,
)
from services.basket_builder import build_shopping_result
from services.dietary import apply_exclusions
from services.nutrition import NutritionService


def parse_variety_mode(value: str | None) -> VarietyMode:
    try:
        return VarietyMode(value) if value else VarietyMode.BALANCED
    except ValueError:
        return VarietyMode.BALANCED


def collect_staples(plan: MealPlan, recipes_by_id: dict[str, Recipe]) -> tuple[str, ...]:
    """Deduped low-quantity seasoning/garnish names from the plan's recipes.

    Only true seasonings (is_seasoning) go here — never a core ingredient, major
    fat, dairy, flour, meaningful sauce, or vegetable. Names are humanized from
    the resolved ingredient or its raw text.
    """
    names: list[str] = []
    seen: set[str] = set()
    seed_recipe_ids: set[str] = set()
    for day in plan.days:
        for meal in day.meals:
            for rid in (meal.recipe_id, meal.side_recipe_id):
                if rid:
                    seed_recipe_ids.add(rid)
    for rid in sorted(seed_recipe_ids):
        recipe = recipes_by_id.get(rid)
        if recipe is None:
            continue
        for ing in recipe.ingredients:
            if not ing.is_seasoning:
                continue
            label = _staple_label(ing.normalized_id, ing.raw_text)
            key = label.lower()
            if key and key not in seen:
                seen.add(key)
                names.append(label)
    return tuple(sorted(names))


def _overlay_leftovers(plan: MealPlan, preassigned) -> MealPlan:
    """Replace the generated meal at each preassigned (day, slot) with the
    prepared-leftover ready meal (which draws no raw ingredients)."""
    from models.meals import DayPlan
    by_slot: dict[tuple[int, str], object] = {
        (pa.day_index, pa.slot.value): pa.meal for pa in preassigned
    }
    new_days = []
    for day in plan.days:
        meals = []
        for meal in day.meals:
            replacement = by_slot.get((day.day_index, meal.slot.value))
            meals.append(replacement if replacement is not None else meal)
        new_days.append(DayPlan(day_index=day.day_index, meals=tuple(meals)))
    return MealPlan(days=tuple(new_days), pantry_carryover=plan.pantry_carryover,
                    consumed_totals=plan.consumed_totals, horizon_days=plan.horizon_days)


def _staple_label(normalized_id: str | None, raw_text: str) -> str:
    # Prefer the clean canonical name; only fall back to trimmed raw text for
    # ingredients that never resolved to a registry id.
    if normalized_id:
        return normalized_id.replace("_", " ")
    import re
    text = re.sub(r"^[\d/.\s\-–()]+", "", raw_text).strip()
    text = re.sub(r"[;,].*$", "", text)  # drop trailing clauses
    text = re.sub(r"\b(to taste|optional|for .*)$", "", text, flags=re.I).strip(" .,")
    return text[:32].lower()


@dataclass(frozen=True)
class RecipeFirstOutput:
    meal_plan: MealPlan
    result: OptimizationResult
    staples: tuple[str, ...]
    variety_mode: VarietyMode
    repair_stats: RepairStats = RepairStats()


def generate_recipe_first(
    recipes: tuple[Recipe, ...],
    foods_by_id: dict,
    profile: HouseholdProfile,
    nutrition: NutritionService,
    pantry_items: dict[str, float],
    quotes: dict,
    budget: float,
    horizon_days: int,
    variety_mode: VarietyMode,
    preassigned=(),
    config: RecipePlanConfig = RecipePlanConfig(),
) -> RecipeFirstOutput:
    """Run the recipe-first pipeline. Raises PlanGenerationError on failure.

    ``preassigned`` prepared-leftover meals (from assign_prepared_leftovers) are
    overlaid onto their slots so they draw zero raw ingredients. Whole-plan
    budget repair runs AFTER the overlay (so it sees the real purchase demand)
    and the plan is finalized unconditionally so carryover/consumed totals
    always reflect the final days; the shopping basket is built last.
    """
    plan = build_recipe_plan(
        recipes, foods_by_id, profile, nutrition, pantry_items, quotes,
        budget, horizon_days, variety_mode, config,
    )
    if preassigned:
        plan = _overlay_leftovers(plan, preassigned)
    context = build_planner_context(recipes, foods_by_id, profile, nutrition)
    days, stats = _repair_budget(
        plan.days, horizon_days, pantry_items, foods_by_id, quotes, budget,
        profile, context, variety_mode, config,
    )
    plan = finalize_meal_plan(days, horizon_days, pantry_items)
    demand = ingredient_demand(plan)
    _, excluded = apply_exclusions(list(foods_by_id.values()), profile)
    result = build_shopping_result(
        demand, pantry_items, foods_by_id, quotes, profile, nutrition,
        budget, horizon_days, excluded,
    )
    if result.budget_status is BudgetStatus.OVER and stats.attempted:
        if stats.swaps_applied > 0:
            message = (f"Swapped {stats.swaps_applied} meal(s) for cheaper options, "
                       f"but the plan still exceeds the budget.")
        else:
            message = ("We looked for cheaper meal substitutions, but none fit "
                       "this plan's nutrition and variety requirements.")
        result = replace(result, relaxed_constraints=result.relaxed_constraints + (message,))
    staples = collect_staples(plan, {r.id: r for r in recipes})
    return RecipeFirstOutput(plan, result, staples, variety_mode, stats)


__all__ = [
    "parse_variety_mode", "collect_staples",
    "generate_recipe_first", "RecipeFirstOutput", "PlanGenerationError",
    "VarietyMode",
]
