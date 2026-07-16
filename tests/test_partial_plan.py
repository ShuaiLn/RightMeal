from dataclasses import replace

import pytest

from data.loader import load_catalog, load_recipe_index
from models import (
    BudgetChoiceRequired,
    PackageOffer,
    PackageOption,
    PartialFoodCoverageCandidate,
    PriceSource,
    RequiredIngredientUnmapped,
    StandardPlanReady,
)
from models.meals import DayPlan, Meal, MealPlan, MealPortion, MealSlot
from models.profile import HouseholdProfile
from models.pricing import seed_package_offers
from planner.demand import ingredient_demand
from planner.partial_plan import (
    PARTIAL_PLAN_WARNING,
    build_partial_food_coverage_plan,
    partial_repeat_limit,
    validate_partial_food_coverage,
)
from planner.recipe_scheduler import RepairStats, VarietyMode
from services import planner_engine
from services.basket_builder import build_shopping_result
from services.planner_engine import RecipeFirstOutput


def complete_candidate(foods_by_id, nutrition, profile, days=2):
    target = nutrition.household_daily_targets(profile)
    food = replace(
        foods_by_id["rice_white"],
        id="partial_test_food",
        package_options=(PackageOption("100 g", 100.0, 1.0),),
        nutrients_per_100g=target.scaled(1 / 3),
    )
    result = []
    for day_index in range(days):
        meals = tuple(
            Meal(
                slot=slot,
                template_id="",
                name=f"Meal {slot.value}",
                portions=(MealPortion(food, 100.0),),
                recipe_id=f"recipe-{day_index}-{slot.value}",
                source_kind="recipe",
                servings=float(profile.total_members),
                household_member_count=profile.total_members,
                full_serving_equivalent=float(profile.total_members),
                portion_scale=1.0,
            )
            for slot in (MealSlot.BREAKFAST, MealSlot.LUNCH, MealSlot.DINNER)
        )
        result.append(DayPlan(day_index, meals))
    return MealPlan(days=tuple(result), horizon_days=days)


def test_partial_plan_keeps_people_days_and_three_meals_at_daily_sixty_percent(
    foods_by_id, nutrition
):
    profile = HouseholdProfile(adults=5)
    complete = complete_candidate(foods_by_id, nutrition, profile, days=3)
    partial, coverage = build_partial_food_coverage_plan(
        complete, profile, nutrition
    )
    assert len(partial.days) == 3
    assert all(len(day.meals) == 3 for day in partial.days)
    for day, daily in zip(partial.days, coverage):
        assert daily.portion_scale == pytest.approx(0.60)
        assert daily.calories_ratio == pytest.approx(0.60)
        assert daily.protein_ratio == pytest.approx(0.60)
        for meal in day.meals:
            assert meal.household_member_count == 5
            assert meal.full_serving_equivalent == pytest.approx(3.0)
            assert meal.portion_scale == pytest.approx(0.60)
            # Actual per-person nutrition uses five people, not three full-
            # serving equivalents.
            assert meal.per_person_kcal == pytest.approx(meal.kcal / 5)
    validate_partial_food_coverage(partial, coverage)
    assert PARTIAL_PLAN_WARNING == (
        "This is not a complete food plan. Additional food is required."
    )


def test_partial_repeat_policy_relaxes_only_from_two_to_three(
    foods_by_id, nutrition
):
    profile = HouseholdProfile(adults=1)
    plan = complete_candidate(foods_by_id, nutrition, profile, days=3)
    # Three uses of one recipe fail max-two but pass the one allowed relaxation.
    repeated_days = []
    for day in plan.days:
        meals = list(day.meals)
        meals[0] = replace(meals[0], recipe_id="same-breakfast")
        repeated_days.append(replace(day, meals=tuple(meals)))
    repeated = replace(plan, days=tuple(repeated_days))
    assert partial_repeat_limit(repeated) == 3

    four_days = complete_candidate(foods_by_id, nutrition, profile, days=4)
    repeated_days = []
    for day in four_days.days:
        meals = list(day.meals)
        meals[0] = replace(meals[0], recipe_id="same-breakfast")
        repeated_days.append(replace(day, meals=tuple(meals)))
    assert partial_repeat_limit(replace(four_days, days=tuple(repeated_days))) is None


def test_typed_orchestration_reprices_and_returns_cap_fitting_partial_candidate(
    foods_by_id, nutrition, monkeypatch
):
    profile = HouseholdProfile(adults=5)
    complete = complete_candidate(foods_by_id, nutrition, profile, days=1)
    food = complete.days[0].meals[0].portions[0].food
    catalog = {food.id: food}
    offer = PackageOffer.for_catalog_package(
        food,
        food.package_options[0],
        price_cents=100,
        source=PriceSource.SEED_ESTIMATE,
        store="Test store",
    )
    quotes = {food.id: (offer,)}
    full_result = build_shopping_result(
        ingredient_demand(complete),
        {},
        catalog,
        quotes,
        profile,
        nutrition,
        2.50,
        1,
    )
    assert full_result.total_cost_cents == 300
    output = RecipeFirstOutput(
        complete,
        full_result,
        (),
        VarietyMode.BALANCED,
        RepairStats(),
    )
    monkeypatch.setattr(planner_engine, "generate_recipe_first", lambda *args, **kwargs: output)
    unused_recipe_issue = RequiredIngredientUnmapped(
        affected_count=148,
        recipe_ids=("unused-incomplete-recipe",),
        detail="an unused recipe has an unmapped required ingredient",
    )
    monkeypatch.setattr(
        planner_engine,
        "collect_recipe_data_issues",
        lambda *args, **kwargs: (unused_recipe_issue,),
    )

    outcome = planner_engine.generate_recipe_first_outcome(
        recipes=(),
        foods_by_id=catalog,
        profile=profile,
        nutrition=nutrition,
        pantry_items={},
        quotes=quotes,
        budget=2.50,
        horizon_days=1,
        variety_mode=VarietyMode.BALANCED,
    )

    assert isinstance(outcome, PartialFoodCoverageCandidate)
    assert outcome.estimated_total_cents == 200
    assert outcome.estimated_cap_cents == 250
    # Two packages fund 66%; 67% crosses the 200 g boundary and needs a third.
    assert outcome.daily_portion_scales == (0.66,)
    assert outcome.minimum_daily_calorie_coverage == pytest.approx(0.66)
    assert outcome.minimum_daily_protein_coverage == pytest.approx(0.66)
    assert outcome.remaining_budget_cents == 50
    assert outcome.next_increment_total_cents == 300
    assert outcome.next_increment_additional_cents == 100
    assert outcome.candidate.meal_plan is outcome.meal_plan
    assert outcome.candidate.result.total_cost_cents == 200
    assert outcome.data_issues == (unused_recipe_issue,)

    without_partial = planner_engine.generate_recipe_first_outcome(
        recipes=(),
        foods_by_id=catalog,
        profile=profile,
        nutrition=nutrition,
        pantry_items={},
        quotes=quotes,
        budget=2.50,
        horizon_days=1,
        variety_mode=VarietyMode.BALANCED,
        allow_partial_food_coverage=False,
    )
    assert isinstance(without_partial, BudgetChoiceRequired)
    assert without_partial.estimated_total_cents == 300
    assert without_partial.data_issues == (unused_recipe_issue,)


def test_partial_optimizer_raises_days_fairly_until_the_next_package_step(
    foods_by_id, nutrition, monkeypatch
):
    profile = HouseholdProfile(adults=5)
    complete = complete_candidate(foods_by_id, nutrition, profile, days=2)
    food = complete.days[0].meals[0].portions[0].food
    catalog = {food.id: food}
    offer = PackageOffer.for_catalog_package(
        food,
        food.package_options[0],
        price_cents=100,
        source=PriceSource.SEED_ESTIMATE,
        store="Test store",
    )
    quotes = {food.id: (offer,)}
    full_result = build_shopping_result(
        ingredient_demand(complete),
        {},
        catalog,
        quotes,
        profile,
        nutrition,
        5.50,
        2,
    )
    assert full_result.total_cost_cents == 600
    output = RecipeFirstOutput(
        complete,
        full_result,
        (),
        VarietyMode.BALANCED,
        RepairStats(),
    )
    monkeypatch.setattr(
        planner_engine,
        "generate_recipe_first",
        lambda *args, **kwargs: output,
    )
    monkeypatch.setattr(
        planner_engine,
        "collect_recipe_data_issues",
        lambda *args, **kwargs: (),
    )

    outcome = planner_engine.generate_recipe_first_outcome(
        recipes=(),
        foods_by_id=catalog,
        profile=profile,
        nutrition=nutrition,
        pantry_items={},
        quotes=quotes,
        budget=5.50,
        horizon_days=2,
        variety_mode=VarietyMode.BALANCED,
    )

    assert isinstance(outcome, PartialFoodCoverageCandidate)
    assert outcome.daily_portion_scales == (0.83, 0.83)
    assert outcome.estimated_total_cents == 500
    assert outcome.remaining_budget_cents == 50
    assert outcome.next_increment_total_cents == 600


def test_three_person_seven_day_200_cap_returns_complete_real_catalog_plan(nutrition):
    foods = {food.id: food for food in load_catalog()}
    quotes = {
        food_id: seed_package_offers(food)
        for food_id, food in foods.items()
    }
    outcome = planner_engine.generate_recipe_first_outcome(
        recipes=load_recipe_index(),
        foods_by_id=foods,
        profile=HouseholdProfile(
            adults=3,
            city="Los Angeles",
            zip_code="90001",
        ),
        nutrition=nutrition,
        pantry_items={},
        quotes=quotes,
        budget=200.0,
        horizon_days=7,
        variety_mode=VarietyMode.BALANCED,
    )

    assert isinstance(outcome, StandardPlanReady)
    assert outcome.estimated_total_cents <= 20_000
    assert outcome.search_limits.algorithm == "budget-aware-beam"
