"""Recipe-first engine tests: scheduler, validators, similarity, basket, and
the acceptance matrix on a real generated week."""

from __future__ import annotations

import pytest

from data.loader import load_catalog, load_recipe_index, load_nutrient_targets
from models.basket import BudgetStatus
from models.profile import HouseholdProfile
from models.meals import SOURCE_RECIPE
from services.nutrition import NutritionService
from planner.recipe_scheduler import VarietyMode, build_recipe_plan
from planner.demand import ingredient_demand
from planner.similarity import is_similar, similarity_score
from services.basket_builder import build_shopping_result


@pytest.fixture(scope="module")
def catalog():
    foods = load_catalog()
    return {f.id: f for f in foods}, load_recipe_index()


@pytest.fixture(scope="module")
def nutrition():
    return NutritionService(load_nutrient_targets())


@pytest.fixture
def family():
    return HouseholdProfile(adults=2, children=2, seniors=0, city="LA", zip_code="90001")


def _plan(catalog, nutrition, family, mode=VarietyMode.BALANCED, pantry=None, budget=250.0):
    foods, recipes = catalog
    return build_recipe_plan(recipes, foods, family, nutrition, pantry or {}, None,
                             budget, 7, mode)


# -- acceptance matrix ------------------------------------------------------

def test_generates_full_week(catalog, nutrition, family):
    plan = _plan(catalog, nutrition, family)
    assert len(plan.days) == 7
    for day in plan.days:
        assert len(day.meals) == 3


def test_every_meal_is_a_real_recipe_with_source(catalog, nutrition, family):
    _, recipes = catalog
    by_id = {recipe.id: recipe for recipe in recipes}
    plan = _plan(catalog, nutrition, family)
    for day in plan.days:
        for meal in day.meals:
            assert meal.source_kind == SOURCE_RECIPE
            assert meal.recipe_id in by_id
            assert by_id[meal.recipe_id].source_file  # traceable to content/


def test_no_fake_plate_names(catalog, nutrition, family):
    plan = _plan(catalog, nutrition, family)
    for day in plan.days:
        for meal in day.meals:
            assert "plate with" not in meal.name.lower()


def test_single_main_carb_unless_allowed(catalog, nutrition, family):
    foods, recipes = catalog
    by_id = {r.id: r for r in recipes}
    plan = _plan(catalog, nutrition, family)
    for day in plan.days:
        for meal in day.meals:
            recipe = by_id[meal.recipe_id]
            carb_ids = {p.food.id for p in meal.portions
                        if p.food.id in recipe.main_carbs}
            if not recipe.allow_multiple_main_carbs:
                assert len(carb_ids) <= 1, (recipe.id, carb_ids)


def test_breakfast_differs_from_lunch_and_no_repeat_in_a_day(catalog, nutrition, family):
    plan = _plan(catalog, nutrition, family)
    for day in plan.days:
        ids = [m.recipe_id for m in day.meals]
        assert len(ids) == len(set(ids)), ("repeat within a day", ids)


def test_per_person_kcal_and_servings_present(catalog, nutrition, family):
    plan = _plan(catalog, nutrition, family)
    for day in plan.days:
        for meal in day.meals:
            assert meal.servings == family.total_members
            assert meal.per_person_kcal is not None and meal.per_person_kcal > 0


def test_daily_calories_reasonable(catalog, nutrition, family):
    plan = _plan(catalog, nutrition, family)
    members = family.total_members
    target = nutrition.household_daily_targets(family).calories_kcal / members
    for day in plan.days:
        per_person = sum(m.kcal for m in day.meals) / members
        # Within a generous band; the daily repair aims for +/-10% but the pool
        # can be thin — never absurd.
        assert 0.6 * target <= per_person <= 1.4 * target, (day.day_index, per_person, target)


def test_balanced_no_same_recipe_consecutive_days(catalog, nutrition, family):
    plan = _plan(catalog, nutrition, family, mode=VarietyMode.BALANCED)
    for slot_idx in range(3):
        for d in range(len(plan.days) - 1):
            a = plan.days[d].meals[slot_idx].recipe_id
            b = plan.days[d + 1].meals[slot_idx].recipe_id
            # Same recipe may appear in different slots, but consecutive-day same
            # slot repeats are disallowed for the same recipe id across the day.
            assert not (a == b), (slot_idx, d, a)


def test_high_variety_uses_each_recipe_at_most_once(catalog, nutrition, family):
    plan = _plan(catalog, nutrition, family, mode=VarietyMode.HIGH_VARIETY)
    seen: dict[str, int] = {}
    for day in plan.days:
        for meal in day.meals:
            seen[meal.recipe_id] = seen.get(meal.recipe_id, 0) + 1
    assert all(c == 1 for c in seen.values()), [k for k, v in seen.items() if v > 1]


def test_deterministic(catalog, nutrition, family):
    a = _plan(catalog, nutrition, family)
    b = _plan(catalog, nutrition, family)
    names_a = [m.name for d in a.days for m in d.meals]
    names_b = [m.name for d in b.days for m in d.meals]
    assert names_a == names_b


def test_vegetarian_profile_excludes_meat(catalog, nutrition):
    foods, _ = catalog
    veg = HouseholdProfile(adults=2, children=0, seniors=0, vegetarian=True,
                           city="LA", zip_code="90001")
    plan = _plan(catalog, nutrition, veg)
    for day in plan.days:
        for meal in day.meals:
            for p in meal.portions:
                assert foods[p.food.id].vegetarian, (meal.name, p.food.id)


def test_pantry_shrinks_the_basket(catalog, nutrition, family):
    foods, recipes = catalog
    plan_empty = _plan(catalog, nutrition, family)
    demand = ingredient_demand(plan_empty)
    # Stock the top-demand food generously.
    top = max(demand, key=demand.get)
    from tests.conftest import make_seed_quote  # type: ignore
    quotes = {f.id: make_seed_quote(f) for f in foods.values()}
    empty = build_shopping_result(demand, {}, foods, quotes, family, nutrition, 250.0, 7)
    stocked = build_shopping_result(demand, {top: demand[top]}, foods, quotes, family,
                                    nutrition, 250.0, 7)
    assert stocked.total_cost <= empty.total_cost


def test_basket_conservation(catalog, nutrition, family):
    foods, recipes = catalog
    plan = _plan(catalog, nutrition, family)
    demand = ingredient_demand(plan)
    from tests.conftest import make_seed_quote  # type: ignore
    quotes = {f.id: make_seed_quote(f) for f in foods.values()}
    result = build_shopping_result(demand, {}, foods, quotes, family, nutrition, 250.0, 7)
    # Every purchased food covers at least its demand (package rounding only overshoots).
    bought: dict[str, float] = {}
    for item in result.items:
        bought[item.food.id] = bought.get(item.food.id, 0.0) + item.grams
    for fid, need in demand.items():
        if fid in bought:
            assert bought[fid] + 0.05 >= need, (fid, bought[fid], need)


def test_over_tight_budget_is_flagged_not_truncated(catalog, nutrition, family):
    foods, recipes = catalog
    plan = _plan(catalog, nutrition, family)
    demand = ingredient_demand(plan)
    from tests.conftest import make_seed_quote  # type: ignore
    quotes = {f.id: make_seed_quote(f) for f in foods.values()}
    result = build_shopping_result(demand, {}, foods, quotes, family, nutrition, 1.0, 7)
    assert result.budget_status is BudgetStatus.OVER
    assert result.relaxed_constraints  # honest message, plan not truncated
    assert result.distinct_foods == len([f for f in demand if demand[f] > 0.05 and f in foods])


# -- similarity -------------------------------------------------------------

def test_similarity_same_recipe_is_one(catalog, nutrition, family):
    _, recipes = catalog
    r = recipes[0]
    assert similarity_score(r, r) == 1.0


def test_baked_dishes_not_auto_similar(catalog):
    _, recipes = catalog
    # Two different baked dishes with different protein/carb should not be similar
    # purely for sharing "baking".
    baked = [r for r in recipes if "baking" in r.cooking_methods and r.auto_plannable]
    pairs = [(a, b) for a in baked for b in baked
             if a.id < b.id and a.main_protein != b.main_protein]
    if pairs:
        a, b = pairs[0]
        assert not is_similar(a, b) or a.dish_category == b.dish_category
