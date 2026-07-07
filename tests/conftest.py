"""Shared fixtures for the RightMeal test suite."""

import pytest

from data import load_nutrient_targets, load_seed_foods
from models import HouseholdProfile
from services.nutrition import NutritionService


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
