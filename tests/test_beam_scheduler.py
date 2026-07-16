"""Focused contracts for the bounded deterministic recipe beam."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from data.loader import load_catalog, load_nutrient_targets
from models.food import Nutrients, PackageOption
from models.meals import Meal, MealSlot
from models.planning import NoPlanFoundWithinSearchLimits, SearchLimits
from models.pricing import PackageOffer, PriceSource
from models.profile import HouseholdProfile
from models.recipe import Recipe, RecipeIngredient, RecipeType
from planner.leftover_prepass import PreassignedMeal
import planner.recipe_scheduler as recipe_scheduler
from planner.recipe_scheduler import (
    VarietyMode,
    build_recipe_plan_with_stats,
)
from services.nutrition import NutritionService
from services.planner_engine import generate_recipe_first_outcome


def _recipe(recipe_id: str, food_id: str, protein: str, dish: str) -> Recipe:
    ingredient = RecipeIngredient(
        raw_text=f"300 g {food_id}",
        canonical_food_id=food_id,
        normalized_id=food_id,
        role="protein",
        grams_per_serving=300.0,
        quantity_state="raw",
        nutrition_basis=None,
        is_core=True,
        is_seasoning=False,
        optional=False,
        match_method="manual",
        confidence=1.0,
    )
    return Recipe(
        id=recipe_id,
        canonical_name=recipe_id.replace("_", " ").title(),
        source_file=f"{recipe_id}.md",
        tags=(),
        recipe_type=RecipeType.MAIN_MEAL,
        meal_types=("breakfast", "lunch", "dinner"),
        cuisine="international",
        dish_category=dish,
        cooking_methods=(dish,),
        servings=1,
        prep_time_min=10,
        cook_time_min=10,
        image_asset=None,
        directions=("Cook.",),
        ingredients=(ingredient,),
        main_protein=protein,
        main_carbs=(),
        allow_multiple_main_carbs=False,
        vegetables=(),
        substitutions=(),
        batchable=False,
        recommended_batch_servings=None,
        leftover_storage_days=None,
        reheat_method=None,
        nutrition_per_serving=Nutrients(calories_kcal=700.0, protein_g=35.0),
        coverage_by_mass=1.0,
        core_coverage=1.0,
        auto_plannable=True,
        contains_pork=False,
        is_meat_or_fish=True,
        allergen_tags=frozenset(),
        verified=True,
    )


def _inputs():
    foods = {food.id: food for food in load_catalog()}
    recipes = (
        _recipe("alpha", "chicken_breast", "chicken", "plate"),
        _recipe("bravo", "ground_beef", "beef", "bowl"),
        _recipe("charlie", "salmon_fillet", "salmon", "skillet"),
    )
    profile = HouseholdProfile(adults=1, city="Los Angeles", zip_code="90001")
    nutrition = NutritionService(load_nutrient_targets())
    return foods, recipes, profile, nutrition


def _signature(plan) -> tuple[str, ...]:
    return tuple(meal.recipe_id for day in plan.days for meal in day.meals)


def test_beam_is_deterministic_and_reports_only_actual_bounded_work():
    foods, recipes, profile, nutrition = _inputs()
    limits = SearchLimits(beam_width=2, max_candidates_per_slot=2)

    first = build_recipe_plan_with_stats(
        recipes, foods, profile, nutrition, {}, None, 100.0, 1,
        VarietyMode.BALANCED, search_limits=limits,
    )
    second = build_recipe_plan_with_stats(
        tuple(reversed(recipes)), foods, profile, nutrition, {}, None, 100.0, 1,
        VarietyMode.BALANCED, search_limits=limits,
    )

    assert _signature(first.meal_plan) == _signature(second.meal_plan)
    assert first.search_stats == second.search_stats
    # One root expands <=2 candidates, then <=2 parents each expand <=2 for
    # each remaining slot: 2 + 4 + 4 actual expansions at most.
    assert 0 < first.search_stats.candidate_count <= 10
    assert first.search_stats.pruned_state_count > 0
    assert first.search_stats.passes_run == 1
    assert not first.search_stats.relaxation_attempted


def test_strict_variety_exhausts_before_bounded_rolling_repeat_relaxation():
    foods, recipes, profile, nutrition = _inputs()
    result = build_recipe_plan_with_stats(
        recipes,
        foods,
        profile,
        nutrition,
        {},
        None,
        100.0,
        2,
        VarietyMode.HIGH_VARIETY,
        search_limits=SearchLimits(beam_width=2, max_candidates_per_slot=3),
    )

    uses = Counter(_signature(result.meal_plan))
    assert set(uses.values()) == {2}
    assert result.search_stats.passes_run == 2
    assert result.search_stats.relaxation_attempted
    assert result.search_stats.relaxation_used


def test_beam_exhaustion_is_only_a_search_limit_outcome():
    profile = HouseholdProfile(adults=1, city="Los Angeles", zip_code="90001")
    outcome = generate_recipe_first_outcome(
        (),
        {},
        profile,
        NutritionService(load_nutrient_targets()),
        {},
        {},
        100.0,
        7,
        VarietyMode.BALANCED,
        search_limits=SearchLimits(beam_width=1, max_candidates_per_slot=1),
    )

    assert isinstance(outcome, NoPlanFoundWithinSearchLimits)
    assert outcome.search_limits.beam_width == 1
    assert outcome.search_limits.max_candidates_per_slot == 1
    assert outcome.search_limits.search_exhaustive is False
    assert outcome.search_limits.candidate_count == 0
    assert outcome.search_limits.pruned_state_count == 0


def test_budget_aware_beam_prefers_quality_only_when_the_packages_fit(monkeypatch):
    foods, _, profile, nutrition = _inputs()
    base = foods["chicken_breast"]
    meal_nutrients = replace(base.nutrients_per_100g, calories_kcal=266.7)
    cheap_package = PackageOption("2 kg", 2000.0, 1.0)
    premium_package = PackageOption("2 kg", 2000.0, 10.0)
    cheap_food = replace(
        base,
        id="beam_cheap_food",
        name="Beam cheap food",
        nutrients_per_100g=meal_nutrients,
        package_options=(cheap_package,),
    )
    premium_food = replace(
        base,
        id="beam_premium_food",
        name="Beam premium food",
        nutrients_per_100g=meal_nutrients,
        package_options=(premium_package,),
    )
    catalog = {cheap_food.id: cheap_food, premium_food.id: premium_food}
    recipes = tuple(
        _recipe(f"cheap-{index}", cheap_food.id, f"cheap-{index}", f"cheap-{index}")
        for index in range(3)
    ) + tuple(
        _recipe(
            f"premium-{index}",
            premium_food.id,
            f"premium-{index}",
            f"premium-{index}",
        )
        for index in range(3)
    )
    quotes = {
        cheap_food.id: (PackageOffer.for_catalog_package(
            cheap_food,
            cheap_package,
            price_cents=100,
            source=PriceSource.SEED_ESTIMATE,
            store="Test",
        ),),
        premium_food.id: (PackageOffer.for_catalog_package(
            premium_food,
            premium_package,
            price_cents=1000,
            source=PriceSource.SEED_ESTIMATE,
            store="Test",
        ),),
    }
    monkeypatch.setattr(
        recipe_scheduler,
        "_score",
        lambda recipe, *args, **kwargs: 10.0 if recipe.id.startswith("premium") else 1.0,
    )
    limits = SearchLimits(beam_width=8, max_candidates_per_slot=6)

    tight = build_recipe_plan_with_stats(
        recipes, catalog, profile, nutrition, {}, quotes, 1.0, 1,
        VarietyMode.BALANCED, search_limits=limits,
    )
    roomy = build_recipe_plan_with_stats(
        tuple(reversed(recipes)), catalog, profile, nutrition, {}, quotes, 10.0, 1,
        VarietyMode.BALANCED, search_limits=limits,
    )

    assert all(recipe_id.startswith("cheap") for recipe_id in _signature(tight.meal_plan))
    assert all(recipe_id.startswith("premium") for recipe_id in _signature(roomy.meal_plan))


def test_preassigned_leftover_occupies_its_slot_during_search():
    foods, recipes, profile, nutrition = _inputs()
    fixed = Meal(
        slot=MealSlot.LUNCH,
        template_id="",
        name="Prepared leftovers",
        portions=(),
        servings=1.0,
        is_leftover=True,
        prepared_leftover_id="leftover-1",
    )
    assignment = PreassignedMeal(0, MealSlot.LUNCH, fixed, "leftover-1", 1.0)

    result = build_recipe_plan_with_stats(
        recipes,
        foods,
        profile,
        nutrition,
        {},
        None,
        100.0,
        1,
        VarietyMode.BALANCED,
        preassigned=(assignment,),
    )

    assert result.meal_plan.days[0].meal_for(MealSlot.LUNCH) is fixed
    assert result.search_stats.candidate_count < 3 * SearchLimits().max_candidates_per_slot
