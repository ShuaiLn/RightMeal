"""Recipe-first meal plan generator.

Filters real catalog recipes, scores them (nutrition/pantry/time/variety), and
uses a deterministic budget-aware bounded beam to schedule the complete
horizon. The user-selected variety policy is searched first. A second bounded
pass, with recipe repetition relaxed to at most two uses in any rolling seven
days, is considered only when the strict pass has no fully-priced candidate
inside the cap. Every candidate still passes dietary, allergy, meal-structure,
portion, and provenance checks before entering the beam.

Budget repair (``_repair_budget``) remains a whole-plan fallback: while the
plan's known real cost exceeds the budget, it swaps the most expensive
swappable meals for cheaper same-slot recipes that pass the full
validation/variety/calorie/quality gates. Known limitation: the repair is
greedy and single-swap-per-round — it cannot find savings that only appear
from swapping two meals together. It never raises for budget infeasibility
(that is reported via ``BudgetStatus.OVER``); real programming or data errors
propagate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from data.loader import load_portion_rules
from models.food import Food, Nutrients
from models.meals import (
    DayPlan, Meal, MealPlan, MealPortion, MealSlot, SLOT_ORDER, SOURCE_RECIPE,
)
from models.planning import SearchLimits
from models.profile import HouseholdProfile
from models.pricing import PackageOffer, PriceQuote, dollars_to_cents
from models.recipe import Recipe, RecipeType
from planner.daily_validator import evaluate_day
from planner.demand import ingredient_demand
from planner.meal_validator import PlanContext, validate_meal
from planner.similarity import identical_core_structure, similarity_score
from services.basket_builder import (
    COST_EPSILON, GRAM_EPSILON, price_demand, price_slice,
)
from services.dietary import recipe_exclusion_reason
from services.nutrition import NutritionService
from services.pantry_flow import meal_draw_grams

KCAL_EPSILON = 1.0  # kcal per person per day — tolerance drift below this is noise


class VarietyMode(str, Enum):
    HIGH_VARIETY = "high_variety"
    BALANCED = "balanced"
    MEAL_PREP = "meal_prep"


@dataclass(frozen=True)
class ScheduleSearchStats:
    """Observed work from the deterministic scheduling beam.

    ``candidate_count`` is the number of candidate meals actually expanded.
    ``pruned_state_count`` counts candidate states discarded by meal
    validation, the per-parent candidate cap, or the global beam-width cap.
    Recipes rejected before expansion by hard variety and same-day rules are
    not candidate states. Budget-repair scans are deliberately excluded.
    """

    candidate_count: int = 0
    pruned_state_count: int = 0
    passes_run: int = 0
    relaxation_attempted: bool = False
    relaxation_used: bool = False

    def plus(
        self,
        other: "ScheduleSearchStats",
        *,
        relaxation_used: bool = False,
    ) -> "ScheduleSearchStats":
        return ScheduleSearchStats(
            candidate_count=self.candidate_count + other.candidate_count,
            pruned_state_count=self.pruned_state_count + other.pruned_state_count,
            passes_run=self.passes_run + other.passes_run,
            relaxation_attempted=(
                self.relaxation_attempted or other.relaxation_attempted
            ),
            relaxation_used=relaxation_used,
        )


class PlanGenerationError(Exception):
    """No valid plan could be built; ``reasons`` explains why (honest failure)."""

    def __init__(
        self,
        message: str,
        reasons: Sequence[str] = (),
        *,
        search_stats: ScheduleSearchStats = ScheduleSearchStats(),
    ):
        super().__init__(message)
        self.reasons = list(reasons)
        self.search_stats = search_stats


@dataclass(frozen=True)
class RecipePlanConfig:
    w_nutrition: float = 1.0
    w_pantry: float = 0.6
    w_time: float = 0.2
    w_cuisine_variety: float = 0.3
    p_repeat: float = 1.5
    p_similarity: float = 1.2
    p_protein_repeat: float = 0.5
    p_carb_repeat: float = 0.4
    similarity_threshold: float = 0.65
    max_daily_repair_attempts: int = 4
    # Budget repair. None rounds -> max(5, 3 * horizon_days), so a large budget
    # gap on a long plan isn't abandoned after 5 swaps; an explicit int (tests)
    # overrides. The same-slot-once rule plus cost-must-improve keeps the loop
    # tight regardless.
    budget_repair_max_rounds: int | None = None
    budget_repair_meals_per_round: int = 6
    budget_repair_candidates_per_meal: int = 3  # must PASS per meal
    budget_repair_candidate_scan_cap: int = 12  # scanned before giving up on a meal
    budget_repair_max_score_regression: float = 0.5


@dataclass
class _History:
    used_counts: dict[str, int] = field(default_factory=dict)
    last_day_used: dict[str, int] = field(default_factory=dict)
    protein_last_day: dict[str, int] = field(default_factory=dict)
    carb_last_day: dict[str, int] = field(default_factory=dict)
    cuisine_last_day: dict[str, int] = field(default_factory=dict)
    recipe_days: dict[str, list[int]] = field(default_factory=dict)

    def record(self, recipe: Recipe, day_index: int) -> None:
        self.used_counts[recipe.id] = self.used_counts.get(recipe.id, 0) + 1
        self.last_day_used[recipe.id] = day_index
        self.recipe_days.setdefault(recipe.id, []).append(day_index)
        if recipe.main_protein:
            self.protein_last_day[recipe.main_protein] = day_index
        for c in recipe.main_carbs:
            self.carb_last_day[c] = day_index
        self.cuisine_last_day[recipe.cuisine] = day_index

    def clone(self) -> "_History":
        return _History(
            used_counts=dict(self.used_counts),
            last_day_used=dict(self.last_day_used),
            protein_last_day=dict(self.protein_last_day),
            carb_last_day=dict(self.carb_last_day),
            cuisine_last_day=dict(self.cuisine_last_day),
            recipe_days={rid: list(days) for rid, days in self.recipe_days.items()},
        )


@dataclass(frozen=True)
class PlannerContext:
    """Shared, read-only generation inputs used by both initial scheduling and
    budget repair — one implementation, no drift. ``frozen=True`` alone would
    not protect the mappings inside, so they are MappingProxyType with tuple
    pool values: a repair bug cannot silently mutate shared generation state."""

    members: int
    portion_rules: dict
    per_person_daily_kcal: float
    recipes_by_id: Mapping[str, Recipe]
    pool: Mapping[str, tuple[Recipe, ...]]


def build_planner_context(
    recipes: Sequence[Recipe],
    foods_by_id: dict[str, Food],
    profile: HouseholdProfile,
    nutrition: NutritionService,
) -> PlannerContext:
    members = max(profile.total_members, 1)
    portion_rules = load_portion_rules()
    daily = nutrition.household_daily_targets(profile)
    per_person_daily_kcal = daily.calories_kcal / members if daily.calories_kcal else 2000.0
    recipes_by_id = {r.id: r for r in recipes}
    pool = _eligible_pool(recipes, profile, foods_by_id)
    return PlannerContext(
        members=members,
        portion_rules=portion_rules,
        per_person_daily_kcal=per_person_daily_kcal,
        recipes_by_id=MappingProxyType(recipes_by_id),
        pool=MappingProxyType({slot: tuple(rs) for slot, rs in pool.items()}),
    )


def finalize_meal_plan(
    days: Sequence[DayPlan], horizon_days: int, pantry_items: dict[str, float],
) -> MealPlan:
    """The single place all ``days``-derived MealPlan state is computed
    (pantry_carryover and consumed_totals today; future fields get added here
    once). Call it on the FINAL days — after overlay and budget repair."""
    carryover = _carryover(pantry_items, days)
    consumed = Nutrients()
    for d in days:
        for m in d.meals:
            consumed = consumed.plus(m.nutrients)
    return MealPlan(days=tuple(days), pantry_carryover=carryover,
                    consumed_totals=consumed, horizon_days=horizon_days)


@dataclass(frozen=True)
class RecipePlanSearchResult:
    meal_plan: MealPlan
    search_stats: ScheduleSearchStats


@dataclass(frozen=True)
class _BeamState:
    days: tuple[DayPlan, ...]
    day_meals: tuple[Meal, ...]
    chosen_recipes: tuple[Recipe, ...]
    history: _History
    virtual_pantry: dict[str, float]
    score: float
    demand: dict[str, float]
    food_cost_cents: dict[str, int]
    cost_cents: int
    unpriced_food_ids: frozenset[str]


@dataclass(frozen=True)
class _BeamPassResult:
    meal_plan: MealPlan | None
    search_stats: ScheduleSearchStats
    failure_reason: str = ""
    within_budget: bool = False


def build_recipe_plan(
    recipes: Sequence[Recipe],
    foods_by_id: dict[str, Food],
    profile: HouseholdProfile,
    nutrition: NutritionService,
    pantry_items: dict[str, float],
    quotes: dict | None,
    budget: float,
    horizon_days: int,
    variety_mode: VarietyMode = VarietyMode.BALANCED,
    config: RecipePlanConfig = RecipePlanConfig(),
    search_limits: SearchLimits = SearchLimits(),
    preassigned=(),
) -> MealPlan:
    """Build a complete plan using the deterministic bounded scheduler.

    This compatibility API continues to return only ``MealPlan``.  Orchestration
    that must expose observed search work uses ``build_recipe_plan_with_stats``.
    """

    return build_recipe_plan_with_stats(
        recipes,
        foods_by_id,
        profile,
        nutrition,
        pantry_items,
        quotes,
        budget,
        horizon_days,
        variety_mode,
        config,
        search_limits,
        preassigned=preassigned,
    ).meal_plan


def build_recipe_plan_with_stats(
    recipes: Sequence[Recipe],
    foods_by_id: dict[str, Food],
    profile: HouseholdProfile,
    nutrition: NutritionService,
    pantry_items: dict[str, float],
    quotes: dict | None,
    budget: float,
    horizon_days: int,
    variety_mode: VarietyMode = VarietyMode.BALANCED,
    config: RecipePlanConfig = RecipePlanConfig(),
    search_limits: SearchLimits = SearchLimits(),
    preassigned=(),
) -> RecipePlanSearchResult:
    """Return the highest-quality fully-priced plan inside ``budget``.

    Fixed prepared leftovers participate in their real slots during search.
    When no strict in-cap plan survives the bounded beam, one relaxed pass is
    allowed.  An over-cap strict candidate is retained over an over-cap relaxed
    candidate for the later repair and partial-coverage fallbacks.
    """

    context = build_planner_context(recipes, foods_by_id, profile, nutrition)
    sides = _side_pool(recipes, profile, foods_by_id)
    fixed_slots = _preassigned_slots(preassigned, horizon_days)
    price_cache: dict[tuple[str, float], tuple[int, bool]] = {}
    rough_cost_hints = {
        recipe.id: _rough_recipe_cost_hint(recipe, context.members, quotes)
        for recipes_for_slot in context.pool.values()
        for recipe in recipes_for_slot
    }
    strict = _beam_schedule_pass(
        context,
        sides,
        foods_by_id,
        profile,
        pantry_items,
        quotes,
        dollars_to_cents(budget),
        horizon_days,
        variety_mode,
        config,
        search_limits,
        fixed_slots,
        price_cache,
        rough_cost_hints,
        relax_repeats=False,
    )
    if strict.meal_plan is not None and (strict.within_budget or not quotes):
        return RecipePlanSearchResult(strict.meal_plan, strict.search_stats)

    relaxed = _beam_schedule_pass(
        context,
        sides,
        foods_by_id,
        profile,
        pantry_items,
        quotes,
        dollars_to_cents(budget),
        horizon_days,
        variety_mode,
        config,
        search_limits,
        fixed_slots,
        price_cache,
        rough_cost_hints,
        relax_repeats=True,
    )
    relaxed_selected = (
        relaxed.meal_plan is not None
        and (relaxed.within_budget or strict.meal_plan is None)
    )
    combined = strict.search_stats.plus(
        relaxed.search_stats,
        relaxation_used=relaxed_selected,
    )
    if relaxed_selected:
        return RecipePlanSearchResult(relaxed.meal_plan, combined)
    if strict.meal_plan is not None:
        return RecipePlanSearchResult(strict.meal_plan, combined)

    reason = (
        relaxed.failure_reason
        or strict.failure_reason
        or "the bounded beam exhausted"
    )
    raise PlanGenerationError(
        _FAIL_MSG,
        reasons=[
            f"{reason}; no complete plan was found after strict variety and "
            "the bounded rolling-seven-day repeat relaxation"
        ],
        search_stats=combined,
    )


def _beam_schedule_pass(
    context: PlannerContext,
    sides: Sequence[Recipe],
    foods_by_id: dict[str, Food],
    profile: HouseholdProfile,
    pantry_items: dict[str, float],
    quotes: dict | None,
    cap_cents: int,
    horizon_days: int,
    variety_mode: VarietyMode,
    config: RecipePlanConfig,
    search_limits: SearchLimits,
    fixed_slots: Mapping[tuple[int, MealSlot], Meal],
    price_cache: dict[tuple[str, float], tuple[int, bool]],
    rough_cost_hints: Mapping[str, float],
    *,
    relax_repeats: bool,
) -> _BeamPassResult:
    """Run one bounded deterministic beam pass.

    At each slot, every retained parent expands at most
    ``max_candidates_per_slot`` candidates selected between quality and exact
    projected package cost. The global beam likewise preserves quality-leading and
    cost-leading in-cap states, plus one cheapest non-actionable state for
    repair or honest failure reporting.  The stable signature makes ties
    independent of input iteration order.
    """

    candidate_count = 0
    pruned_state_count = 0
    initial = _BeamState(
        days=(),
        day_meals=(),
        chosen_recipes=(),
        history=_History(),
        virtual_pantry=dict(pantry_items),
        score=0.0,
        demand={},
        food_cost_cents={},
        cost_cents=0,
        unpriced_food_ids=frozenset(),
    )
    beam: list[_BeamState] = [initial]
    total_slots = max(0, horizon_days) * len(SLOT_ORDER)
    failure_reason = ""

    for slot_index in range(total_slots):
        day_index, within_day = divmod(slot_index, len(SLOT_ORDER))
        slot = SLOT_ORDER[within_day]
        next_states: list[_BeamState] = []

        for state in beam:
            plan_ctx = PlanContext(
                profile,
                slot,
                day_index,
                context.per_person_daily_kcal,
                context.portion_rules,
                context.recipes_by_id,
                foods_by_id,
            )
            fixed_meal = fixed_slots.get((day_index, slot))
            if fixed_meal is not None:
                expansions: list[tuple[float, Recipe | None, Meal]] = [
                    (0.0, None, fixed_meal)
                ]
            else:
                scored = _scored_candidates_for_slot(
                    slot,
                    context.pool[slot.value],
                    context.members,
                    context.per_person_daily_kcal,
                    state.history,
                    state.day_meals,
                    state.virtual_pantry,
                    variety_mode,
                    config,
                    plan_ctx,
                    relax_repeats=relax_repeats,
                )
                bounded = _budget_balanced_candidates(
                    scored,
                    state,
                    slot,
                    foods_by_id,
                    context.members,
                    variety_mode,
                    pantry_items,
                    quotes,
                    search_limits.max_candidates_per_slot,
                    price_cache,
                    rough_cost_hints,
                )
                pruned_state_count += len(scored) - len(bounded)
                expansions = [
                    (
                        score,
                        recipe,
                        _build_meal(
                            recipe,
                            slot,
                            foods_by_id,
                            context.members,
                            variety_mode,
                        ),
                    )
                    for score, _recipe_id, recipe in bounded
                ]

            for score, recipe, meal in expansions:
                if recipe is not None:
                    candidate_count += 1
                    if validate_meal(meal, plan_ctx):
                        pruned_state_count += 1
                        continue

                new_day_meals = state.day_meals + (meal,)
                unrepaired_day_meals = new_day_meals
                new_chosen = state.chosen_recipes + (
                    ((recipe,) if recipe is not None else ())
                )
                new_days = state.days
                new_history = state.history
                new_pantry = state.virtual_pantry

                if within_day == len(SLOT_ORDER) - 1:
                    repaired = tuple(_repair_day_calories(
                        list(new_day_meals),
                        profile,
                        context.per_person_daily_kcal,
                        sides,
                        foods_by_id,
                        context.members,
                        config,
                        context.recipes_by_id,
                    ))
                    new_days = state.days + (
                        DayPlan(day_index=day_index, meals=repaired),
                    )
                    new_history = state.history.clone()
                    for chosen in new_chosen:
                        new_history.record(chosen, day_index)
                    new_pantry = dict(state.virtual_pantry)
                    for repaired_meal in repaired:
                        _draw(new_pantry, repaired_meal)
                    new_day_meals = ()
                    new_chosen = ()

                replacements = (
                    tuple(zip(unrepaired_day_meals, repaired))
                    if within_day == len(SLOT_ORDER) - 1
                    else ()
                )
                (
                    new_demand,
                    new_food_costs,
                    new_cost_cents,
                    new_unpriced,
                ) = _advance_beam_pricing(
                    state,
                    meal,
                    pantry_items,
                    foods_by_id,
                    quotes,
                    price_cache,
                    replacements=replacements,
                )

                next_states.append(_BeamState(
                    days=new_days,
                    day_meals=new_day_meals,
                    chosen_recipes=new_chosen,
                    history=new_history,
                    virtual_pantry=new_pantry,
                    score=state.score + score,
                    demand=new_demand,
                    food_cost_cents=new_food_costs,
                    cost_cents=new_cost_cents,
                    unpriced_food_ids=new_unpriced,
                ))

        if not next_states:
            failure_reason = (
                f"day {day_index + 1} {slot.value}: the bounded beam had no "
                "candidate state that passed validation"
            )
            stats = ScheduleSearchStats(
                candidate_count=candidate_count,
                pruned_state_count=pruned_state_count,
                passes_run=1,
                relaxation_attempted=relax_repeats,
            )
            return _BeamPassResult(
                meal_plan=None,
                search_stats=stats,
                failure_reason=failure_reason,
            )

        selected = _select_budget_aware_beam(
            next_states,
            search_limits.beam_width,
            cap_cents,
        )
        pruned_state_count += len(next_states) - len(selected)
        beam = selected

    affordable = [state for state in beam if _state_within_budget(state, cap_cents)]
    if affordable:
        winner = min(affordable, key=_beam_state_sort_key)
        within_budget = True
    else:
        winner = min(beam, key=_beam_fallback_sort_key)
        within_budget = False
    stats = ScheduleSearchStats(
        candidate_count=candidate_count,
        pruned_state_count=pruned_state_count,
        passes_run=1,
        relaxation_attempted=relax_repeats,
        relaxation_used=relax_repeats,
    )
    return _BeamPassResult(
        meal_plan=finalize_meal_plan(winner.days, horizon_days, pantry_items),
        search_stats=stats,
        failure_reason=failure_reason,
        within_budget=within_budget,
    )


def _preassigned_slots(
    preassigned: Sequence[object],
    horizon_days: int,
) -> Mapping[tuple[int, MealSlot], Meal]:
    """Validate and freeze fixed prepared-leftover slots for the beam."""

    slots: dict[tuple[int, MealSlot], Meal] = {}
    for assignment in preassigned:
        day_index = int(getattr(assignment, "day_index"))
        slot = getattr(assignment, "slot")
        meal = getattr(assignment, "meal")
        if not isinstance(slot, MealSlot) or not isinstance(meal, Meal):
            raise ValueError("preassigned entries require a MealSlot and Meal")
        if day_index < 0 or day_index >= horizon_days:
            raise ValueError("preassigned meal day is outside the planning horizon")
        if meal.slot is not slot:
            raise ValueError("preassigned meal slot does not match its assignment")
        key = (day_index, slot)
        if key in slots:
            raise ValueError("only one preassigned meal is allowed per slot")
        slots[key] = meal
    return MappingProxyType(slots)


def _demand_with_meal(demand: dict[str, float], meal: Meal) -> dict[str, float]:
    result = dict(demand)
    for food_id, grams in meal_draw_grams(meal).items():
        result[food_id] = result.get(food_id, 0.0) + grams
    return result


def _advance_beam_pricing(
    state: _BeamState,
    meal: Meal,
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: dict | None,
    price_cache: dict[tuple[str, float], tuple[int, bool]],
    *,
    replacements: Sequence[tuple[Meal, Meal]] = (),
) -> tuple[dict[str, float], dict[str, int], int, frozenset[str]]:
    """Add one slot and exactly reprice only foods whose demand changed."""

    demand = _demand_with_meal(state.demand, meal)
    changed_ids = set(meal_draw_grams(meal))
    for old_meal, new_meal in replacements:
        if old_meal == new_meal:
            continue
        changed_ids.update(meal_draw_grams(old_meal))
        changed_ids.update(meal_draw_grams(new_meal))
        demand = apply_meal_demand_delta(demand, old_meal, new_meal)

    food_costs = dict(state.food_cost_cents)
    unpriced = set(state.unpriced_food_ids)
    for food_id in changed_ids:
        food_costs.pop(food_id, None)
        unpriced.discard(food_id)
        grams = demand.get(food_id, 0.0)
        if grams <= GRAM_EPSILON:
            continue
        cache_key = (food_id, round(grams, 3))
        cached = price_cache.get(cache_key)
        if cached is None:
            priced = price_slice(
                {food_id: grams},
                pantry_items,
                foods_by_id,
                quotes,
            )
            cached = (
                dollars_to_cents(priced.total_cost),
                food_id in priced.unpriced_food_ids,
            )
            price_cache[cache_key] = cached
        food_costs[food_id] = cached[0]
        if cached[1]:
            unpriced.add(food_id)
    return demand, food_costs, sum(food_costs.values()), frozenset(unpriced)


def _budget_balanced_candidates(
    scored: Sequence[tuple[float, str, Recipe]],
    state: _BeamState,
    slot: MealSlot,
    foods_by_id: dict[str, Food],
    members: int,
    variety_mode: VarietyMode,
    pantry_items: dict[str, float],
    quotes: dict | None,
    limit: int,
    price_cache: dict[tuple[str, float], tuple[int, bool]],
    rough_cost_hints: Mapping[str, float],
) -> list[tuple[float, str, Recipe]]:
    """Bound one parent's expansion without discarding all cheap recipes."""

    if len(scored) <= limit:
        return list(scored)
    # Exact package optimization is intentionally limited to a deterministic
    # cheap shortlist. The rough hint only creates that shortlist; the rows
    # admitted as cost leaders are ordered by exact cumulative state cost.
    rough_ranked = sorted(
        scored,
        key=lambda row: (
            rough_cost_hints.get(row[1], 0.0),
            row[1],
        ),
    )
    cheap_shortlist = rough_ranked[: min(len(rough_ranked), max(limit, limit * 2))]
    projected: list[tuple[int, bool, float, str, tuple[float, str, Recipe]]] = []
    for row in cheap_shortlist:
        score, recipe_id, recipe = row
        meal = _build_meal(recipe, slot, foods_by_id, members, variety_mode)
        _, _, cost_cents, unpriced = _advance_beam_pricing(
            state,
            meal,
            pantry_items,
            foods_by_id,
            quotes,
            price_cache,
        )
        projected.append((cost_cents, bool(unpriced), score, recipe_id, row))

    quality_ranked = list(scored)
    cost_ranked = [
        entry[4]
        for entry in sorted(
            projected,
            key=lambda entry: (entry[1], entry[0], -entry[2], entry[3]),
        )
    ]
    quality_slots = (limit + 1) // 2
    cost_slots = limit - quality_slots
    selected: dict[str, tuple[float, str, Recipe]] = {}
    for row in quality_ranked[:quality_slots]:
        selected[row[1]] = row
    for row in cost_ranked[:cost_slots]:
        selected[row[1]] = row
    for ranked in (quality_ranked, cost_ranked):
        for row in ranked:
            if len(selected) >= limit:
                break
            selected.setdefault(row[1], row)
    return sorted(selected.values(), key=lambda row: (-row[0], row[1]))


def _state_within_budget(state: _BeamState, cap_cents: int) -> bool:
    return not state.unpriced_food_ids and state.cost_cents <= cap_cents


def _beam_fallback_sort_key(state: _BeamState) -> tuple:
    return (
        state.cost_cents,
        bool(state.unpriced_food_ids),
        len(state.unpriced_food_ids),
        -state.score,
        _beam_state_signature(state),
    )


def _select_budget_aware_beam(
    states: Sequence[_BeamState],
    beam_width: int,
    cap_cents: int,
) -> list[_BeamState]:
    affordable = [state for state in states if _state_within_budget(state, cap_cents)]
    fallback = [state for state in states if not _state_within_budget(state, cap_cents)]
    if not affordable:
        return [min(fallback, key=_beam_fallback_sort_key)]

    reserve_fallback = bool(fallback) and beam_width > 1
    affordable_limit = beam_width - int(reserve_fallback)
    quality_ranked = sorted(affordable, key=_beam_state_sort_key)
    cost_ranked = sorted(
        affordable,
        key=lambda state: (
            state.cost_cents,
            -state.score,
            _beam_state_signature(state),
        ),
    )
    quality_slots = (affordable_limit + 1) // 2
    cost_slots = affordable_limit - quality_slots
    selected: list[_BeamState] = []
    selected_ids: set[int] = set()

    def add(state: _BeamState) -> None:
        if id(state) not in selected_ids and len(selected) < affordable_limit:
            selected_ids.add(id(state))
            selected.append(state)

    for state in quality_ranked[:quality_slots]:
        add(state)
    for state in cost_ranked[:cost_slots]:
        add(state)
    for ranked in (quality_ranked, cost_ranked):
        for state in ranked:
            add(state)
    if reserve_fallback:
        selected.append(min(fallback, key=_beam_fallback_sort_key))
    return selected


def _beam_state_signature(state: _BeamState) -> tuple[tuple[int, str, str, str], ...]:
    signature: list[tuple[int, str, str, str]] = []
    for day in state.days:
        for meal in day.meals:
            signature.append((
                day.day_index,
                meal.slot.value,
                meal.recipe_id or "",
                meal.side_recipe_id or "",
            ))
    day_index = len(state.days)
    for meal in state.day_meals:
        signature.append((
            day_index,
            meal.slot.value,
            meal.recipe_id or "",
            meal.side_recipe_id or "",
        ))
    return tuple(signature)


def _beam_state_sort_key(state: _BeamState) -> tuple:
    return (-state.score, state.cost_cents, _beam_state_signature(state))


_FAIL_MSG = (
    "Unable to create a valid meal plan with the current calorie target, dietary "
    "restrictions, budget, and available verified recipes."
)


def _eligible_pool(recipes, profile, foods_by_id) -> dict[str, list[Recipe]]:
    pool: dict[str, list[Recipe]] = {"breakfast": [], "lunch": [], "dinner": []}
    for r in sorted(recipes, key=lambda x: x.id):
        if not r.auto_plannable:
            continue
        if r.recipe_type not in (RecipeType.MAIN_MEAL, RecipeType.BREAKFAST):
            continue
        if recipe_exclusion_reason(r, profile, foods_by_id):
            continue
        for slot in r.meal_types:
            if slot in pool:
                pool[slot].append(r)
    return pool


def _side_pool(recipes, profile, foods_by_id) -> list[Recipe]:
    return [
        r for r in sorted(recipes, key=lambda x: x.id)
        if r.auto_plannable
        and r.recipe_type in (RecipeType.SIDE, RecipeType.MAIN_MEAL, RecipeType.BREAKFAST)
        and not recipe_exclusion_reason(r, profile, foods_by_id)
    ]


def _pick_for_slot(slot, candidates, foods_by_id, members, per_person_daily_kcal,
                   portion_rules, history, day_meals, virtual_pantry, variety_mode,
                   config, ctx):
    scored = _scored_candidates_for_slot(
        slot,
        candidates,
        members,
        per_person_daily_kcal,
        history,
        day_meals,
        virtual_pantry,
        variety_mode,
        config,
        ctx,
        relax_repeats=False,
    )
    for _, _, recipe in scored:
        meal = _build_meal(recipe, slot, foods_by_id, members, variety_mode)
        if not validate_meal(meal, ctx):
            return recipe, meal
    return None, None


def _scored_candidates_for_slot(
    slot,
    candidates,
    members,
    per_person_daily_kcal,
    history,
    day_meals,
    virtual_pantry,
    variety_mode,
    config,
    ctx,
    *,
    relax_repeats: bool,
):
    slot_share = ctx.portion_rules["slot_kcal_share_midpoint"][slot.value]
    slot_target = per_person_daily_kcal * slot_share
    today_ids = {m.recipe_id for m in day_meals}
    prev_meal = day_meals[-1] if day_meals else None
    today_recipes = [ctx.recipes_by_id[m.recipe_id] for m in day_meals if m.recipe_id in ctx.recipes_by_id]

    scored: list[tuple[float, str, Recipe]] = []
    for r in candidates:
        if r.id in today_ids:
            continue
        if not _variety_ok(
            r,
            history,
            prev_meal,
            ctx,
            variety_mode,
            relax_repeats=relax_repeats,
        ):
            continue
        score = _score(r, slot_target, members, history, today_recipes,
                       virtual_pantry, config)
        scored.append((score, r.id, r))

    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored


def _variety_ok(
    r,
    history,
    prev_meal,
    ctx,
    mode,
    *,
    relax_repeats: bool = False,
) -> bool:
    used = history.used_counts.get(r.id, 0)
    last_day = history.last_day_used.get(r.id)
    if relax_repeats:
        window_start = ctx.day_index - 6
        rolling_uses = sum(
            day >= window_start for day in history.recipe_days.get(r.id, ())
        )
        if rolling_uses >= 2:
            return False
    elif mode == VarietyMode.HIGH_VARIETY:
        if used >= 1:
            return False
    elif mode == VarietyMode.MEAL_PREP:
        if used >= 3:
            return False
    else:  # BALANCED
        if last_day is not None and last_day == ctx.day_index - 1:
            return False
    if prev_meal is not None and prev_meal.recipe_id:
        prev = ctx.recipes_by_id.get(prev_meal.recipe_id)
        if prev is not None and identical_core_structure(prev, r):
            return False
    return True


def _score(r, slot_target, members, history, today_recipes, virtual_pantry, config) -> float:
    per_person_kcal = r.nutrition_per_serving.calories_kcal
    denom = slot_target if slot_target > 0 else 1.0
    nutrition_fit = 1.0 - min(abs(per_person_kcal - slot_target) / denom, 1.0)

    total_g = cov_g = 0.0
    for ing in r.ingredients:
        if (
            getattr(ing, "is_nonfood", False)
            or not ing.canonical_food_id
            or ing.grams_per_serving is None
        ):
            continue
        g = ing.grams_per_serving * members
        total_g += g
        cov_g += min(g, virtual_pantry.get(ing.canonical_food_id, 0.0))
    pantry_cov = (cov_g / total_g) if total_g else 0.0

    total_time = (r.prep_time_min or 0) + (r.cook_time_min or 0)
    time_score = 1.0 - min(total_time / 120.0, 1.0)

    score = (config.w_nutrition * nutrition_fit
             + config.w_pantry * pantry_cov
             + config.w_time * time_score)

    # Cuisine variety bonus when this cuisine hasn't appeared recently.
    if r.cuisine != "international" and history.cuisine_last_day.get(r.cuisine) is None:
        score += config.w_cuisine_variety

    # Repeat / rotation penalties.
    score -= config.p_repeat * history.used_counts.get(r.id, 0)
    if r.main_protein and history.protein_last_day.get(r.main_protein) is not None:
        score -= config.p_protein_repeat
    if any(history.carb_last_day.get(c) is not None for c in r.main_carbs):
        score -= config.p_carb_repeat

    # Similarity penalty against today's other meals.
    for other in today_recipes:
        s = similarity_score(r, other)
        if s >= config.similarity_threshold:
            score -= config.p_similarity * s
    return score


def _build_meal(recipe, slot, foods_by_id, members, variety_mode, side=None) -> Meal:
    portions = _portions_for_recipe(recipe, foods_by_id, members, "main")
    name = recipe.canonical_name
    side_servings = 0.0
    if side is not None:
        portions += _portions_for_recipe(side, foods_by_id, members, "side")
        name = f"{recipe.canonical_name} with {side.canonical_name}"
        side_servings = float(members)
    batch = (variety_mode == VarietyMode.MEAL_PREP and recipe.batchable and slot == MealSlot.DINNER)
    return Meal(
        slot=slot, template_id="", name=name, portions=tuple(portions),
        recipe_id=recipe.id, source_kind=SOURCE_RECIPE, servings=float(members),
        side_recipe_id=side.id if side else None, side_servings=side_servings,
        batch_id=f"batch-{recipe.id}-{slot.value}" if batch else None,
    )


def _portions_for_recipe(recipe, foods_by_id, members, component) -> list[MealPortion]:
    portions: list[MealPortion] = []
    for ing in recipe.ingredients:
        if (
            getattr(ing, "is_nonfood", False)
            or not ing.canonical_food_id
            or ing.is_seasoning
            or ing.optional
        ):
            continue
        if ing.grams_per_serving is None:
            continue
        food = foods_by_id.get(ing.canonical_food_id)
        if food is None:
            continue
        grams = ing.grams_per_serving * members
        cooked = grams * food.cooked_yield_factor if (ing.quantity_state == "dry" and food.cooked_yield_factor) else None
        portions.append(MealPortion(
            food=food, grams=round(grams, 2), cooked_grams=cooked,
            source_recipe_id=recipe.id, component_kind=component))
    return portions


def _repair_day_calories(day_meals, profile, per_person_daily_kcal, sides,
                         foods_by_id, members, config, recipes_by_id) -> list[Meal]:
    for _ in range(config.max_daily_repair_attempts):
        result = evaluate_day(DayPlan(0, tuple(day_meals)), profile, per_person_daily_kcal)
        if result.within_tolerance or result.shortfall_kcal <= 0:
            break
        repairable = [
            index
            for index, candidate in enumerate(day_meals)
            if candidate.recipe_id is not None
            and candidate.side_recipe_id is None
            and not candidate.is_leftover
            and candidate.prepared_leftover_id is None
        ]
        if not repairable:
            break
        idx = min(repairable, key=lambda i: day_meals[i].kcal)
        meal = day_meals[idx]
        primary = recipes_by_id.get(meal.recipe_id)
        if primary is None:
            break
        needed = result.shortfall_kcal * members
        side = _best_side(sides, needed, primary, members)
        if side is None:
            break
        new_meal = _build_meal(primary, meal.slot, foods_by_id, members, VarietyMode.BALANCED, side=side)
        if meal.batch_id:
            new_meal = _with_batch(new_meal, meal.batch_id)
        day_meals[idx] = new_meal
    return day_meals


def _with_batch(meal: Meal, batch_id: str) -> Meal:
    from dataclasses import replace
    return replace(meal, batch_id=batch_id)


def _best_side(sides, needed_kcal, primary, members):
    best = None
    best_gap = None
    for s in sides:
        if primary is not None and (s.id == primary.id or identical_core_structure(primary, s)):
            continue
        gap = abs(s.nutrition_per_serving.calories_kcal * members - needed_kcal)
        if best_gap is None or gap < best_gap:
            best, best_gap = s, gap
    return best


def _draw(virtual_pantry, meal) -> None:
    for food_id, grams in meal_draw_grams(meal).items():
        if food_id in virtual_pantry:
            virtual_pantry[food_id] = max(0.0, virtual_pantry[food_id] - grams)


def _carryover(pantry_items, days) -> dict[str, float]:
    used: dict[str, float] = {}
    for d in days:
        for m in d.meals:
            for fid, g in meal_draw_grams(m).items():
                used[fid] = used.get(fid, 0.0) + g
    carry: dict[str, float] = {}
    for fid, have in pantry_items.items():
        left = have - used.get(fid, 0.0)
        if left > 0.01:
            carry[fid] = round(left, 3)
    return carry


# -- budget repair ------------------------------------------------------------


@dataclass(frozen=True)
class RepairStats:
    attempted: bool = False
    rounds_run: int = 0
    candidates_scanned: int = 0
    candidates_passed: int = 0
    swaps_applied: int = 0
    elapsed_ms: float = 0.0


def pantry_snapshots_by_day(
    pantry_items: dict[str, float], days: Sequence[DayPlan],
) -> dict[int, dict[str, float]]:
    """Pantry stock at the START of each day: day 0's candidates see the
    initial stock, day 6's see stock after six days of draws. Pure."""
    vp = dict(pantry_items)
    out: dict[int, dict[str, float]] = {}
    for day in days:
        out[day.day_index] = dict(vp)
        for m in day.meals:
            _draw(vp, m)
    return out


def apply_meal_demand_delta(
    demand: dict[str, float], old_meal: Meal, new_meal: Meal,
) -> dict[str, float]:
    """A new demand dict with old_meal's draw replaced by new_meal's draw.
    Missing foods read as 0; results at or below GRAM_EPSILON are dropped."""
    old_draw, new_draw = meal_draw_grams(old_meal), meal_draw_grams(new_meal)
    result = dict(demand)
    for fid in set(old_draw) | set(new_draw):
        v = max(0.0, demand.get(fid, 0.0) - old_draw.get(fid, 0.0) + new_draw.get(fid, 0.0))
        if v > GRAM_EPSILON:
            result[fid] = v
        else:
            result.pop(fid, None)
    return result


def _rough_recipe_cost_hint(recipe: Recipe, members: int, quotes: dict | None) -> float:
    """Naive purchase-cost signal for RANKING only, never a gate:
    grams_per_serving x members x normalized unit price over non-seasoning,
    non-optional ingredients; unquoted ingredients contribute 0.0."""
    quotes = quotes or {}
    total = 0.0
    for ing in recipe.ingredients:
        if (
            getattr(ing, "is_nonfood", False)
            or not ing.canonical_food_id
            or ing.is_seasoning
            or ing.optional
        ):
            continue
        if ing.grams_per_serving is None:
            continue
        pricing = quotes.get(ing.canonical_food_id)
        if pricing is None:
            continue
        if isinstance(pricing, PriceQuote):
            cost_per_gram = pricing.normalized_unit_price / 100.0
        else:
            offers = tuple(pricing)
            if not offers or not all(isinstance(o, PackageOffer) for o in offers):
                continue
            cost_per_gram = min(o.price_cents / 100.0 / o.package_grams for o in offers)
        total += ing.grams_per_serving * members * cost_per_gram
    return total


def _history_excluding(
    days: Sequence[DayPlan], ctx: PlannerContext, skip_day_index: int, skip_slot: MealSlot,
) -> _History:
    """A fresh _History replay of the whole plan minus one slot — a soft
    signal for _score only (the hard adjacency gate is checked separately)."""
    history = _History()
    for d in days:
        for m in d.meals:
            if d.day_index == skip_day_index and m.slot is skip_slot:
                continue
            r = ctx.recipes_by_id.get(m.recipe_id) if m.recipe_id else None
            if r is not None:
                history.record(r, d.day_index)
    return history


def _swap_targets(
    days: Sequence[DayPlan], ctx: PlannerContext, quotes: dict | None,
    swapped: set[tuple[int, str]], config: RecipePlanConfig,
) -> list[tuple[DayPlan, Meal, Recipe]]:
    """Swappable meals ranked most-expensive-first by the cost hint, with the
    stable tie-break (-hint, day_index, slot.value); already-swapped slots and
    batch/leftover meals are excluded."""
    ranked: list[tuple[float, int, str, DayPlan, Meal, Recipe]] = []
    for day in days:
        for meal in day.meals:
            if meal.batch_id is not None or meal.is_leftover or meal.prepared_leftover_id is not None:
                continue
            if (day.day_index, meal.slot.value) in swapped:
                continue
            recipe = ctx.recipes_by_id.get(meal.recipe_id) if meal.recipe_id else None
            if recipe is None:
                continue
            hint = _rough_recipe_cost_hint(recipe, ctx.members, quotes)
            ranked.append((-hint, day.day_index, meal.slot.value, day, meal, recipe))
    ranked.sort(key=lambda t: (t[0], t[1], t[2]))
    return [(day, meal, recipe)
            for _, _, _, day, meal, recipe in ranked[:config.budget_repair_meals_per_round]]


def _candidate_recipes(
    day: DayPlan, meal: Meal, ctx: PlannerContext, quotes: dict | None,
) -> list[Recipe]:
    """Same-slot candidates, cheapest hint first. Ranking stays cost-only;
    quality is a separate pass/fail gate (dollars and dimensionless score
    weights have no coherent ordering)."""
    day_recipe_ids = {m.recipe_id for m in day.meals if m.recipe_id}
    pool = ctx.pool.get(meal.slot.value, ())
    candidates = [r for r in pool if r.id not in day_recipe_ids]
    candidates.sort(key=lambda r: (_rough_recipe_cost_hint(r, ctx.members, quotes), r.id))
    return candidates


def _passes_swap(
    cand: Recipe,
    day: DayPlan,
    meal: Meal,
    current_recipe: Recipe,
    days: Sequence[DayPlan],
    snapshots: dict[int, dict[str, float]],
    demand: dict[str, float],
    running_total: float,
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: dict | None,
    profile: HouseholdProfile,
    ctx: PlannerContext,
    variety_mode: VarietyMode,
    config: RecipePlanConfig,
) -> tuple[Meal, float] | None:
    """Full validation of one swap candidate, cheap checks first. Returns
    (trial_meal, trial_total) on pass, None on any failure."""
    slot = meal.slot
    # Side preserved (its cost/calories cancel out of the comparison), real
    # variety mode, and never a new batch meal minted by repair.
    side = ctx.recipes_by_id.get(meal.side_recipe_id) if meal.side_recipe_id else None
    trial = _build_meal(cand, slot, foods_by_id, ctx.members, variety_mode, side=side)
    if trial.batch_id is not None:
        return None

    plan_ctx = PlanContext(profile, slot, day.day_index, ctx.per_person_daily_kcal,
                           ctx.portion_rules, ctx.recipes_by_id, foods_by_id)
    if validate_meal(trial, plan_ctx):
        return None

    # Same-day structural clash (identical_core_structure is symmetric).
    for other in day.meals:
        if other is meal or not other.recipe_id:
            continue
        other_recipe = ctx.recipes_by_id.get(other.recipe_id)
        if other_recipe is not None and identical_core_structure(other_recipe, cand):
            return None

    # Weekly repeat / adjacency written fresh against the actual days, both
    # directions (a _History replay's last-write-wins last_day_used could hide
    # an earlier adjacent-day repeat).
    uses = 0
    adjacent = False
    for d in days:
        for m in d.meals:
            if m.recipe_id != cand.id:
                continue
            if d.day_index == day.day_index and m.slot is slot:
                continue  # the very slot being replaced
            uses += 1
            if abs(d.day_index - day.day_index) == 1:
                adjacent = True
    if variety_mode == VarietyMode.HIGH_VARIETY and uses >= 1:
        return None
    if variety_mode == VarietyMode.MEAL_PREP and uses >= 3:
        return None
    if variety_mode == VarietyMode.BALANCED and adjacent:
        return None

    # Daily calorie tolerance must not get worse (shortfall and surplus are
    # mutually exclusive, so their sum is the day's violation).
    trial_day = DayPlan(day_index=day.day_index,
                        meals=tuple(trial if m is meal else m for m in day.meals))
    pre = evaluate_day(day, profile, ctx.per_person_daily_kcal)
    post = evaluate_day(trial_day, profile, ctx.per_person_daily_kcal)
    pre_violation = pre.shortfall_kcal + pre.surplus_kcal
    post_violation = post.shortfall_kcal + post.surplus_kcal
    if post_violation > pre_violation + KCAL_EPSILON:
        return None

    # Quality guard: the existing _score as pass/fail, scored against THIS
    # day's pantry snapshot so day 0 sees initial stock and day 6 sees stock
    # after six days of draws — both sides use the same snapshot.
    slot_share = ctx.portion_rules["slot_kcal_share_midpoint"][slot.value]
    slot_target = ctx.per_person_daily_kcal * slot_share
    history = _history_excluding(days, ctx, day.day_index, slot)
    today_recipes = [ctx.recipes_by_id[m.recipe_id] for m in day.meals
                     if m is not meal and m.recipe_id in ctx.recipes_by_id]
    pantry_snapshot = snapshots[day.day_index]
    cand_score = _score(cand, slot_target, ctx.members, history, today_recipes,
                        pantry_snapshot, config)
    cur_score = _score(current_recipe, slot_target, ctx.members, history, today_recipes,
                       pantry_snapshot, config)
    if cand_score < cur_score - config.budget_repair_max_score_regression:
        return None

    # Exact delta pricing against CUMULATIVE plan demand: each food is priced
    # independently, so slicing to the changed foods reconstructs exact totals
    # (whether a package drops depends on the plan-wide total, not one meal's
    # grams in isolation).
    changed_ids = set(meal_draw_grams(meal)) | set(meal_draw_grams(trial))
    old_slice = {fid: demand[fid] for fid in changed_ids
                 if demand.get(fid, 0.0) > GRAM_EPSILON}
    new_full = apply_meal_demand_delta(demand, meal, trial)
    new_slice = {fid: new_full[fid] for fid in changed_ids
                 if new_full.get(fid, 0.0) > GRAM_EPSILON}
    old_sp = price_slice(old_slice, pantry_items, foods_by_id, quotes)
    new_sp = price_slice(new_slice, pantry_items, foods_by_id, quotes)
    trial_total = running_total - old_sp.total_cost + new_sp.total_cost
    if trial_total > running_total - COST_EPSILON:
        return None

    # Unpriced-gap rule — compare before/after, don't blanket-reject: sharing
    # a pre-existing unpriced ingredient passes; introducing a new unpriced
    # food or growing a gap is rejected (the "improvement" could be an
    # artifact of unpriced grams).
    if (new_sp.unpriced_food_ids - old_sp.unpriced_food_ids
            or new_sp.unpriced_gap_grams > old_sp.unpriced_gap_grams + GRAM_EPSILON):
        return None

    return trial, trial_total


def _repair_budget(
    days: Sequence[DayPlan],
    horizon_days: int,
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: dict | None,
    budget: float,
    profile: HouseholdProfile,
    ctx: PlannerContext,
    variety_mode: VarietyMode,
    config: RecipePlanConfig = RecipePlanConfig(),
) -> tuple[tuple[DayPlan, ...], RepairStats]:
    """Swap expensive meals for cheaper valid ones while the KNOWN real cost
    exceeds the budget. Pure with respect to its inputs: the passed days are
    never mutated; a new tuple is returned. Acts whenever the known cost is
    over — reducing known cost is always legitimate; the per-candidate gap
    rule forbids WORSENING the unpriced gap, not touching it."""
    start = time.perf_counter()
    days = tuple(days)
    demand = ingredient_demand(MealPlan(days=days, horizon_days=horizon_days))
    priced = price_demand(demand, pantry_items, foods_by_id, quotes)
    if not quotes or priced.total_cost <= budget + COST_EPSILON:
        return days, RepairStats(attempted=False,
                                 elapsed_ms=(time.perf_counter() - start) * 1000.0)

    max_rounds = config.budget_repair_max_rounds
    if max_rounds is None:
        max_rounds = max(5, 3 * horizon_days)

    running_total = priced.total_cost
    swapped: set[tuple[int, str]] = set()  # a slot is swapped at most once per run
    rounds_run = candidates_scanned = candidates_passed = swaps_applied = 0

    for _ in range(max_rounds):
        if running_total <= budget + COST_EPSILON:
            break
        rounds_run += 1
        # Per-day snapshots recomputed each round (a swap changes later days'
        # draws); O(days x meals), cheap.
        snapshots = pantry_snapshots_by_day(pantry_items, days)

        best_key: tuple[float, int, str] | None = None
        best: tuple[DayPlan, Meal, Meal, float] | None = None
        for day, meal, current_recipe in _swap_targets(days, ctx, quotes, swapped, config):
            passed_here = scanned_here = 0
            # Scan in cheapest-hint order, validating each, until K have
            # PASSED or the cap has been SCANNED — unquoted candidates look
            # artificially cheapest, and doomed ones must not monopolize the
            # pass slots.
            for cand in _candidate_recipes(day, meal, ctx, quotes):
                if (passed_here >= config.budget_repair_candidates_per_meal
                        or scanned_here >= config.budget_repair_candidate_scan_cap):
                    break
                scanned_here += 1
                candidates_scanned += 1
                outcome = _passes_swap(
                    cand, day, meal, current_recipe, days, snapshots, demand,
                    running_total, pantry_items, foods_by_id, quotes, profile,
                    ctx, variety_mode, config)
                if outcome is None:
                    continue
                passed_here += 1
                candidates_passed += 1
                trial, trial_total = outcome
                key = (trial_total, day.day_index, meal.slot.value)
                if best_key is None or key < best_key:
                    best_key, best = key, (day, meal, trial, trial_total)

        if best is None:
            break  # nothing passed anywhere — converged
        target_day, old_meal, new_meal, _ = best
        demand = apply_meal_demand_delta(demand, old_meal, new_meal)
        new_meals = tuple(new_meal if m is old_meal else m for m in target_day.meals)
        days = tuple(
            DayPlan(day_index=d.day_index, meals=new_meals) if d is target_day else d
            for d in days)
        swapped.add((target_day.day_index, old_meal.slot.value))
        swaps_applied += 1
        # Re-anchor from a full pricing once per applied swap so incremental
        # float drift can't accumulate; the delta arithmetic above only RANKS
        # candidates within the round.
        running_total = price_demand(demand, pantry_items, foods_by_id, quotes).total_cost

    return days, RepairStats(
        attempted=True,
        rounds_run=rounds_run,
        candidates_scanned=candidates_scanned,
        candidates_passed=candidates_passed,
        swaps_applied=swaps_applied,
        elapsed_ms=(time.perf_counter() - start) * 1000.0,
    )
