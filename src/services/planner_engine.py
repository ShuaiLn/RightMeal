"""The recipe-first meal-plan orchestration.

There is a single planning path: real catalog recipes are filtered, scored, and
validated into a plan, then the shopping basket is built from the plan's
ingredient demand. (The retired template/optimizer engine has been removed.)
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from models.basket import BudgetStatus, OptimizationResult
from models.meals import MealPlan
from models.planning import (
    BudgetChoiceRequired,
    CatalogDataIncomplete,
    DataIssue,
    DataUnavailable,
    NoPlanFoundWithinSearchLimits,
    PackageDataUnavailable,
    PartialFoodCoverageCandidate,
    PlanningOutcome,
    RequiredIngredientUnmapped,
    RequiredPriceUnavailable,
    SearchLimits,
    StandardPlanReady,
)
from models.profile import HouseholdProfile
from models.recipe import Recipe, RecipeType
from models.pricing import dollars_to_cents
from planner.demand import ingredient_demand
from planner.partial_plan import (
    PartialPlanError,
    build_partial_food_coverage_plan,
    partial_repeat_limit,
    scale_partial_food_coverage_plan,
    validate_partial_food_coverage,
)
from planner.recipe_scheduler import (
    PlanGenerationError, RecipePlanConfig, RepairStats, ScheduleSearchStats,
    VarietyMode, _repair_budget, build_planner_context,
    build_recipe_plan_with_stats, finalize_meal_plan,
)
from services.basket_builder import build_shopping_result, offers_for_food
from services.dietary import apply_exclusions, recipe_exclusion_reason
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
    search_stats: ScheduleSearchStats = ScheduleSearchStats()


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
    search_limits: SearchLimits = SearchLimits(),
) -> RecipeFirstOutput:
    """Run the recipe-first pipeline. Raises PlanGenerationError on failure.

    ``preassigned`` prepared-leftover meals (from assign_prepared_leftovers) are
    fixed in their real slots by the scheduler, so search nutrition and cost
    already reflect them. Whole-plan budget repair remains a fallback and the
    shopping basket is built last.
    """
    scheduled = build_recipe_plan_with_stats(
        recipes, foods_by_id, profile, nutrition, pantry_items, quotes,
        budget, horizon_days, variety_mode, config, search_limits,
        preassigned=preassigned,
    )
    plan = scheduled.meal_plan
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
            message = (
                f"Swapped {stats.swaps_applied} meal(s) for cheaper options, "
                "but the plan still exceeds the estimated basket budget cap."
            )
        else:
            message = ("We looked for cheaper meal substitutions, but none fit "
                       "this plan's nutrition and variety requirements.")
        result = replace(result, relaxed_constraints=result.relaxed_constraints + (message,))
    staples = collect_staples(plan, {r.id: r for r in recipes})
    return RecipeFirstOutput(
        plan,
        result,
        staples,
        variety_mode,
        stats,
        scheduled.search_stats,
    )


def collect_recipe_data_issues(
    recipes: tuple[Recipe, ...],
    foods_by_id: dict,
    profile: HouseholdProfile,
    pricing: dict | None = None,
) -> tuple[DataIssue, ...]:
    """Aggregate missing required data for otherwise profile-eligible recipes.

    A successful within-cap candidate may carry these as diagnostics.  When a
    bounded search does not find one, the typed orchestration returns
    ``DataUnavailable`` because these recipes could change that judgment.
    """

    unmapped_recipes: set[str] = set()
    unmapped_foods: set[str] = set()
    catalog_recipes: set[str] = set()
    catalog_foods: set[str] = set()
    package_recipes: set[str] = set()
    package_foods: set[str] = set()
    price_recipes: set[str] = set()
    price_foods: set[str] = set()

    for recipe in recipes:
        if recipe.recipe_type not in (RecipeType.MAIN_MEAL, RecipeType.BREAKFAST):
            continue
        if not recipe.meal_types or recipe_exclusion_reason(recipe, profile, foods_by_id):
            continue
        for ingredient in recipe.ingredients:
            if (
                ingredient.optional
                or ingredient.is_seasoning
                or getattr(ingredient, "is_nonfood", False)
            ):
                continue
            food_id = ingredient.canonical_food_id
            if not food_id:
                unmapped_recipes.add(recipe.id)
                if ingredient.normalized_id:
                    unmapped_foods.add(ingredient.normalized_id)
                continue
            food = foods_by_id.get(food_id)
            if food is None or ingredient.grams_per_serving is None \
                    or ingredient.grams_per_serving <= 0:
                catalog_recipes.add(recipe.id)
                catalog_foods.add(food_id)
                continue
            packages = tuple(getattr(food, "package_options", ()))
            if not packages or not any(
                getattr(package, "grams", 0) > 0 for package in packages
            ):
                package_recipes.add(recipe.id)
                package_foods.add(food_id)
                continue
            if pricing is not None:
                try:
                    usable_offers = offers_for_food(food, pricing.get(food_id))
                except (TypeError, ValueError):
                    usable_offers = ()
                if not usable_offers:
                    price_recipes.add(recipe.id)
                    price_foods.add(food_id)

    issues: list[DataIssue] = []
    if unmapped_recipes:
        issues.append(RequiredIngredientUnmapped(
            affected_count=len(unmapped_recipes),
            recipe_ids=tuple(sorted(unmapped_recipes)),
            food_ids=tuple(sorted(unmapped_foods)),
            detail="required non-optional ingredients have no catalog mapping",
        ))
    if catalog_recipes:
        issues.append(CatalogDataIncomplete(
            affected_count=len(catalog_recipes),
            recipe_ids=tuple(sorted(catalog_recipes)),
            food_ids=tuple(sorted(catalog_foods)),
            detail="required ingredients lack a catalog food or positive grams_per_serving",
        ))
    if package_recipes:
        issues.append(PackageDataUnavailable(
            affected_count=len(package_recipes),
            recipe_ids=tuple(sorted(package_recipes)),
            food_ids=tuple(sorted(package_foods)),
            detail="required mapped foods have no positive-size package",
        ))
    if price_recipes:
        issues.append(RequiredPriceUnavailable(
            affected_count=len(price_recipes),
            recipe_ids=tuple(sorted(price_recipes)),
            food_ids=tuple(sorted(price_foods)),
            detail="required mapped foods have no positive package offer",
        ))
    return tuple(issues)


def _merge_data_issues(issues: tuple[DataIssue, ...]) -> tuple[DataIssue, ...]:
    """Stable de-duplication for preflight and chosen-plan price evidence."""

    seen: set[tuple] = set()
    merged: list[DataIssue] = []
    for issue in issues:
        key = (
            type(issue), issue.recipe_ids, issue.food_ids, issue.detail
        )
        if key not in seen:
            seen.add(key)
            merged.append(issue)
    return tuple(merged)


def _partial_food_coverage_candidate(
    output: RecipeFirstOutput,
    foods_by_id: dict,
    profile: HouseholdProfile,
    nutrition: NutritionService,
    pantry_items: dict[str, float],
    quotes: dict,
    budget: float,
    horizon_days: int,
    search_limits: SearchLimits,
    data_issues: tuple[DataIssue, ...],
) -> PartialFoodCoverageCandidate | None:
    """Maximize a fair partial plan in one-percent, whole-package steps."""

    try:
        _minimum_plan, minimum_coverage = build_partial_food_coverage_plan(
            output.meal_plan,
            profile,
            nutrition,
        )
    except PartialPlanError:
        return None

    if not any(day.portion_scale < 1.0 - 1e-9 for day in minimum_coverage):
        return None
    cap_cents = dollars_to_cents(budget)
    trial = _partial_trial(
        output,
        tuple(day.portion_scale for day in minimum_coverage),
        foods_by_id,
        profile,
        nutrition,
        pantry_items,
        quotes,
        budget,
        horizon_days,
    )
    if trial is None or not _partial_trial_fits(trial, cap_cents):
        return None

    # Phase one is water filling: only the currently worst-covered day or tied
    # days advance, so spare budget cannot leave another day at a lower floor.
    while True:
        limiting = tuple(
            min(day.calories_ratio, day.protein_ratio)
            for day in trial.daily_coverage
        )
        fair_floor = min(limiting)
        worst = tuple(
            index
            for index, value in enumerate(limiting)
            if value <= fair_floor + 1e-9 and trial.scales[index] < 1.0 - 1e-9
        )
        if not worst:
            break
        fair_scales = list(trial.scales)
        for index in worst:
            fair_scales[index] = _next_percent(fair_scales[index])
        candidate = _partial_trial(
            output,
            tuple(fair_scales),
            foods_by_id,
            profile,
            nutrition,
            pantry_items,
            quotes,
            budget,
            horizon_days,
        )
        if candidate is None or not _partial_trial_fits(candidate, cap_cents):
            break
        trial = candidate

    # Phase two spends only affordable one-day steps. Zero-cost package steps
    # win first; positive-cost steps maximize limiting-nutrition gain per cent.
    next_increment_total_cents: int | None = None
    while True:
        affordable: list[tuple[tuple, _PartialTrial]] = []
        priced_next: list[tuple[int, int]] = []
        for day_index, scale in enumerate(trial.scales):
            if scale >= 1.0 - 1e-9:
                continue
            candidate_scales = list(trial.scales)
            candidate_scales[day_index] = _next_percent(scale)
            candidate = _partial_trial(
                output,
                tuple(candidate_scales),
                foods_by_id,
                profile,
                nutrition,
                pantry_items,
                quotes,
                budget,
                horizon_days,
            )
            if candidate is None or candidate.result.unpriced_food_ids:
                continue
            total_cents = candidate.result.total_cost_cents
            priced_next.append((total_cents, day_index))
            if not _partial_trial_fits(candidate, cap_cents):
                continue
            delta_cents = total_cents - trial.result.total_cost_cents
            before = min(
                trial.daily_coverage[day_index].calories_ratio,
                trial.daily_coverage[day_index].protein_ratio,
            )
            after = min(
                candidate.daily_coverage[day_index].calories_ratio,
                candidate.daily_coverage[day_index].protein_ratio,
            )
            gain = max(0.0, after - before)
            key = (
                0 if delta_cents == 0 else 1,
                -gain if delta_cents == 0 else -(gain / delta_cents),
                total_cents,
                day_index,
            )
            affordable.append((key, candidate))
        if not affordable:
            if priced_next:
                next_increment_total_cents = min(priced_next)[0]
            break
        trial = min(affordable, key=lambda item: item[0])[1]

    partial_plan = trial.meal_plan
    daily_coverage = trial.daily_coverage
    partial_result = trial.result
    total_cents = partial_result.total_cost_cents
    rolling_max_uses = partial_repeat_limit(partial_plan)
    if rolling_max_uses is None:
        return None

    partial_output = replace(
        output,
        meal_plan=partial_plan,
        result=partial_result,
    )
    return PartialFoodCoverageCandidate(
        candidate=partial_output,
        meal_plan=partial_plan,
        daily_coverage=daily_coverage,
        estimated_total_cents=total_cents,
        estimated_cap_cents=cap_cents,
        household_member_count=profile.total_members,
        rolling_recipe_max_uses=rolling_max_uses,
        search_limits=search_limits,
        data_issues=data_issues,
        remaining_budget_cents=cap_cents - total_cents,
        next_increment_total_cents=next_increment_total_cents,
    )


@dataclass(frozen=True)
class _PartialTrial:
    meal_plan: MealPlan
    daily_coverage: tuple
    result: OptimizationResult
    scales: tuple[float, ...]


def _next_percent(scale: float) -> float:
    return min(1.0, round(scale + 0.01, 2))


def _partial_trial(
    output: RecipeFirstOutput,
    scales: tuple[float, ...],
    foods_by_id: dict,
    profile: HouseholdProfile,
    nutrition: NutritionService,
    pantry_items: dict[str, float],
    quotes: dict,
    budget: float,
    horizon_days: int,
) -> _PartialTrial | None:
    """Rebuild all derived state for one exact partial scale vector."""

    try:
        plan, coverage = scale_partial_food_coverage_plan(
            output.meal_plan,
            profile,
            nutrition,
            scales,
        )
        plan = finalize_meal_plan(plan.days, horizon_days, pantry_items)
        validate_partial_food_coverage(plan, coverage)
    except PartialPlanError:
        return None
    result = build_shopping_result(
        ingredient_demand(plan),
        pantry_items,
        foods_by_id,
        quotes,
        profile,
        nutrition,
        budget,
        horizon_days,
        output.result.excluded_foods,
    )
    return _PartialTrial(plan, coverage, result, scales)


def _partial_trial_fits(trial: _PartialTrial, cap_cents: int) -> bool:
    return (
        trial.result.budget_status is BudgetStatus.WITHIN
        and not trial.result.unpriced_food_ids
        and trial.result.total_cost_cents <= cap_cents
    )


def _observed_search_limits(
    configured: SearchLimits,
    stats: ScheduleSearchStats,
) -> SearchLimits:
    """Attach actual beam work without inheriting an exhaustive-search claim."""

    return replace(
        configured,
        candidate_count=stats.candidate_count,
        pruned_state_count=stats.pruned_state_count,
        search_exhaustive=False,
        algorithm="budget-aware-beam",
    )


def generate_recipe_first_outcome(
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
    search_limits: SearchLimits = SearchLimits(),
    allow_partial_food_coverage: bool = True,
) -> PlanningOutcome:
    """Backward-compatible generation wrapped in an explicit bounded outcome.

    This adapter intentionally has no branch that constructs
    ``NoFeasiblePlanProven``: bounded beam exhaustion cannot prove mathematical
    infeasibility.  Observed counts describe scheduling work only; the later
    budget-repair heuristic reports its own independent statistics.
    """

    issues = collect_recipe_data_issues(recipes, foods_by_id, profile, quotes)
    try:
        output = generate_recipe_first(
            recipes,
            foods_by_id,
            profile,
            nutrition,
            pantry_items,
            quotes,
            budget,
            horizon_days,
            variety_mode,
            preassigned=preassigned,
            config=config,
            search_limits=search_limits,
        )
    except PlanGenerationError as exc:
        reason = "; ".join(exc.reasons) if exc.reasons else str(exc)
        observed_limits = _observed_search_limits(
            search_limits,
            exc.search_stats,
        )
        if issues:
            return DataUnavailable(
                issues=issues,
                search_limits=observed_limits,
                reason=reason,
            )
        return NoPlanFoundWithinSearchLimits(
            search_limits=observed_limits,
            reason=reason,
        )

    observed_limits = _observed_search_limits(
        search_limits,
        output.search_stats,
    )
    result = output.result
    if result.unpriced_food_ids:
        issues = issues + (RequiredPriceUnavailable(
            affected_count=len(result.unpriced_food_ids),
            food_ids=tuple(sorted(result.unpriced_food_ids)),
            detail="the selected complete candidate contains unpriced purchase gaps",
        ),)
    issues = _merge_data_issues(issues)

    cap_cents = dollars_to_cents(budget)
    total_cents = result.total_cost_cents
    # Selected-candidate price gaps are blocking, but catalog issues belonging
    # only to unused recipes remain diagnostics.  A fully priced candidate is
    # actionable even when incomplete recipes elsewhere in the catalog might
    # have changed the search result: the user can review a partial candidate
    # or explicitly raise the cap instead of reaching a dead end.
    if result.budget_status is BudgetStatus.UNKNOWN:
        if not issues:
            issues = (RequiredPriceUnavailable(
                affected_count=max(1, len(result.unpriced_food_names)),
                detail="the selected complete candidate has incomplete pricing",
            ),)
        return DataUnavailable(
            issues=issues,
            search_limits=observed_limits,
            reason="Required pricing data is unavailable.",
        )
    if total_cents <= cap_cents:
        return StandardPlanReady(
            candidate=output,
            estimated_total_cents=total_cents,
            estimated_cap_cents=cap_cents,
            search_limits=observed_limits,
            data_issues=issues,
        )
    if allow_partial_food_coverage:
        partial = _partial_food_coverage_candidate(
            output,
            foods_by_id,
            profile,
            nutrition,
            pantry_items,
            quotes,
            budget,
            horizon_days,
            observed_limits,
            issues,
        )
        if partial is not None:
            return partial
    return BudgetChoiceRequired(
        candidate=output,
        estimated_total_cents=total_cents,
        estimated_cap_cents=cap_cents,
        search_limits=observed_limits,
        data_issues=issues,
    )


__all__ = [
    "parse_variety_mode", "collect_staples",
    "generate_recipe_first", "RecipeFirstOutput", "PlanGenerationError",
    "generate_recipe_first_outcome", "collect_recipe_data_issues",
    "VarietyMode",
]
