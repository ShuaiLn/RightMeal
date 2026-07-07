"""Shared fixtures for the RightMeal test suite."""

import pytest

from data import load_nutrient_targets, load_seed_foods
from models import HouseholdProfile, PriceQuote, PriceSource
from services.nutrition import NutritionService


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
