"""Shared fixtures for the RightMeal test suite."""

import json

import httpx
import pytest

from data import load_nutrient_targets, load_seed_foods
from models import HouseholdProfile, PriceQuote, PriceSource
from models.food import Nutrients
from models.meals import (
    SOURCE_RECIPE,
    DayPlan,
    Meal,
    MealPlan,
    MealPortion,
    MealSlot,
)
from services.basket_builder import build_shopping_result
from services.nutrition import NutritionService


def openai_client(payload=None, status=200, raise_timeout=False) -> httpx.AsyncClient:
    """A mock OpenAI chat-completions client: returns ``payload`` as the
    message content (JSON-encoded unless already a string), or simulates an
    HTTP error / read timeout."""

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_timeout:
            raise httpx.ReadTimeout("timed out")
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "boom"}})
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_seed_quote(food) -> PriceQuote:
    """Deterministic seed quote for a food (mirrors SeedProvider)."""
    pkg = min(food.package_options, key=lambda p: (food.seed_cost_per_100(p), p.label))
    return PriceQuote(
        food_name=food.name,
        matched_product_name=food.name,
        price=pkg.seed_price,
        unit=pkg.label,
        unit_price=pkg.seed_price,
        normalized_unit_price=food.seed_cost_per_100(pkg),
        raw_unit=pkg.label,
        normalized_unit="100ml" if food.is_liquid else "100g",
        store="Seed data",
        source=PriceSource.SEED_ESTIMATE,
        confidence=1.0,
        is_estimate=True,
        last_updated="2026-01-01T00:00:00",
        match_reason="curated seed estimate",
    )


@pytest.fixture(scope="session")
def seed_quotes():
    return {f.id: make_seed_quote(f) for f in load_seed_foods()}


@pytest.fixture(scope="session")
def foods():
    return load_seed_foods()


@pytest.fixture(scope="session")
def foods_by_id(foods):
    return {f.id: f for f in foods}


@pytest.fixture(scope="session")
def nutrition():
    return NutritionService(load_nutrient_targets())


@pytest.fixture
def la_family_profile():
    """The spec's sample case: Los Angeles, 2 adults + 2 children, no restrictions."""
    return HouseholdProfile(adults=2, children=2, city="Los Angeles", zip_code="90001")


# -- recipe-first result / plan builders (replace the retired optimizer) ------
#
# The legacy optimizer/scheduler is gone. Tests that just need a realistic
# OptimizationResult or MealPlan to exercise a retained feature build them here
# from a demand (via the recipe-first basket builder) or by hand from seed foods.


def result_from_demand(
    demand, profile, budget, horizon, foods_by_id, quotes, nutrition, excluded=None,
    pantry=None,
):
    """An OptimizationResult for a chosen ingredient demand over seed foods."""
    return build_shopping_result(
        dict(demand), dict(pantry or {}), foods_by_id, quotes, profile, nutrition,
        budget, horizon, dict(excluded or {}),
    )


def _recipe_meal(slot, day_index, foods_by_id, mains, sides=()):
    """A recipe-first meal from seed foods: mains tagged component 'main', an
    optional side folded into the same portions list tagged 'side'."""
    recipe_id = f"recipe-{slot.value}-{day_index}"
    portions = [
        MealPortion(
            food=foods_by_id[fid], grams=float(g), source_recipe_id=recipe_id,
            component_kind="main",
        )
        for fid, g in mains
    ]
    side_recipe_id = None
    if sides:
        side_recipe_id = f"side-{slot.value}-{day_index}"
        portions += [
            MealPortion(
                food=foods_by_id[fid], grams=float(g), source_recipe_id=side_recipe_id,
                component_kind="side",
            )
            for fid, g in sides
        ]
    return Meal(
        slot=slot, template_id="", name=f"Recipe {slot.value} {day_index}",
        portions=tuple(portions), recipe_id=recipe_id, source_kind=SOURCE_RECIPE,
        servings=float(max(profile_total(foods_by_id), 4)),
        side_recipe_id=side_recipe_id,
        side_servings=(4.0 if side_recipe_id else 0.0),
        batch_id=(f"batch-{day_index}" if slot is MealSlot.DINNER and day_index == 0 else None),
    )


def profile_total(_foods_by_id) -> int:
    return 4  # the fixture household size these plans assume


def make_recipe_meal_plan(foods_by_id, horizon=7) -> MealPlan:
    """A deterministic recipe-first MealPlan over seed foods for persistence and
    migration tests — every meal has provenance-tagged portions, dinners carry a
    side, and day 0's dinner is a batch."""
    days = []
    consumed = Nutrients()
    for d in range(horizon):
        meals = [
            _recipe_meal(MealSlot.BREAKFAST, d, foods_by_id,
                         [("rolled_oats", 80.0), ("milk_whole", 200.0), ("bananas", 120.0)]),
            _recipe_meal(MealSlot.LUNCH, d, foods_by_id,
                         [("bread_whole_wheat", 120.0), ("eggs_large", 100.0)],
                         sides=[("carrots", 90.0)]),
            _recipe_meal(MealSlot.DINNER, d, foods_by_id,
                         [("rice_white", 150.0), ("chicken_breast", 180.0)],
                         sides=[("broccoli_frozen", 110.0)]),
        ]
        for meal in meals:
            consumed = consumed.plus(meal.nutrients)
        days.append(DayPlan(day_index=d, meals=tuple(meals)))
    return MealPlan(
        days=tuple(days),
        pantry_carryover={"rice_white": 40.0, "canola_oil": 30.0},
        consumed_totals=consumed,
        horizon_days=horizon,
    )
