"""Typed bounded-search outcomes cannot overclaim feasibility."""

import pytest

from models import (
    BudgetChoiceRequired,
    DataUnavailable,
    NoFeasiblePlanProven,
    PartialFoodCoverageCandidate,
    RequiredPriceUnavailable,
    SearchLimits,
    StandardPlanReady,
    is_saveable_outcome,
)
from models.meals import DayPlan, Meal, MealPlan, MealSlot
from planner.partial_plan import DailyFoodCoverage


def test_search_limit_defaults_and_counts_are_deterministic():
    limits = SearchLimits()
    assert limits.beam_width == 32
    assert limits.max_candidates_per_slot == 24
    assert limits.pruned_state_count == 0
    assert limits.candidate_count == 0
    assert limits.with_counts(pruned_state_count=7, candidate_count=19) == SearchLimits(
        pruned_state_count=7,
        candidate_count=19,
    )


def test_beam_exhaustion_cannot_claim_no_feasible_plan_proven():
    with pytest.raises(ValueError, match="exhaustive search or a verified certificate"):
        NoFeasiblePlanProven(search_limits=SearchLimits())

    proven = NoFeasiblePlanProven(
        search_limits=SearchLimits(search_exhaustive=True),
        reason="exhaustive finite search",
    )
    assert proven.search_limits.search_exhaustive


def test_ready_and_budget_choice_enforce_estimated_cap():
    ready = StandardPlanReady(
        candidate=object(), estimated_total_cents=999, estimated_cap_cents=1000
    )
    assert is_saveable_outcome(ready)
    with pytest.raises(ValueError, match="must fit"):
        StandardPlanReady(
            candidate=object(), estimated_total_cents=1001, estimated_cap_cents=1000
        )

    choice = BudgetChoiceRequired(
        candidate=object(), estimated_total_cents=1001, estimated_cap_cents=1000
    )
    assert not is_saveable_outcome(choice)


def test_data_unavailable_requires_specific_issue():
    with pytest.raises(ValueError, match="at least one"):
        DataUnavailable(issues=())
    unavailable = DataUnavailable(
        issues=(RequiredPriceUnavailable(food_ids=("rice",)),)
    )
    assert unavailable.data_issues == unavailable.issues


def test_partial_candidate_is_unsaveable_until_explicit_confirmation():
    meals = tuple(
        Meal(
            slot=slot,
            template_id="",
            name=slot.value,
            portions=(),
            servings=3.0,
            household_member_count=5,
            full_serving_equivalent=3.0,
            portion_scale=0.6,
        )
        for slot in (MealSlot.BREAKFAST, MealSlot.LUNCH, MealSlot.DINNER)
    )
    meal_plan = MealPlan(days=(DayPlan(0, meals),), horizon_days=1)
    partial = PartialFoodCoverageCandidate(
        candidate=object(),
        meal_plan=meal_plan,
        daily_coverage=(DailyFoodCoverage(0, 0.6, 0.61, 0.62),),
        estimated_total_cents=900,
        estimated_cap_cents=1000,
        household_member_count=5,
        rolling_recipe_max_uses=2,
    )
    assert partial.daily_portion_scales == (0.6,)
    assert partial.full_serving_equivalent == pytest.approx(3.0)
    assert partial.minimum_daily_calorie_coverage == pytest.approx(0.61)
    assert partial.minimum_daily_protein_coverage == pytest.approx(0.62)
    assert partial.remaining_budget_cents == 100
    assert partial.next_increment_total_cents is None
    assert not is_saveable_outcome(partial)
    assert is_saveable_outcome(partial, partial_confirmed=True)
