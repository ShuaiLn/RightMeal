"""Structural unused-food categorization tests (no reason-string parsing)."""

import pytest

from models import HouseholdProfile
from planner import UnusedCategory, categorize_unused
from services.dietary import apply_exclusions

from conftest import result_from_demand


@pytest.fixture(scope="module")
def vegetarian_run(foods, foods_by_id, seed_quotes, nutrition):
    profile = HouseholdProfile(
        adults=2, children=1, vegetarian=True, allergies=["peanut"],
        city="Los Angeles", zip_code="90001",
    )
    _, excluded = apply_exclusions(foods, profile)
    # A vegetarian, peanut-free basket demand (never includes an excluded food).
    demand = {"rice_white": 1800.0, "eggs_large": 800.0, "milk_whole": 2500.0,
              "black_beans_dry": 900.0, "carrots": 800.0, "broccoli_frozen": 700.0,
              "bread_whole_wheat": 1000.0, "tofu_firm": 700.0, "bananas": 1000.0}
    result = result_from_demand(demand, profile, 55.0, 7, foods_by_id, seed_quotes,
                                nutrition, excluded=excluded)
    return result, seed_quotes


def test_filter_excluded_foods_are_cannot_use(foods, vegetarian_run):
    result, quotes = vegetarian_run
    categories = categorize_unused(foods, result, quotes)
    cannot_use = {food.id: reason for food, reason in categories[UnusedCategory.CANNOT_USE]}
    assert set(cannot_use) == set(result.excluded_foods)
    for food_id, reason in cannot_use.items():
        assert reason == result.excluded_foods[food_id]
    assert "chicken_breast" in cannot_use  # vegetarian filter
    assert "peanut_butter" in cannot_use  # allergy filter


def test_missing_quote_is_no_reliable_price(foods, vegetarian_run):
    result, quotes = vegetarian_run
    basket_ids = {item.food.id for item in result.items}
    victim = next(
        f.id for f in foods if f.id not in basket_ids and f.id not in result.excluded_foods
    )
    pruned = {fid: quote for fid, quote in quotes.items() if fid != victim}
    categories = categorize_unused(foods, result, pruned)
    no_price = {food.id for food, _ in categories[UnusedCategory.NO_RELIABLE_PRICE]}
    assert no_price == {victim}


def test_quoted_but_unpicked_is_not_selected(foods, vegetarian_run):
    result, quotes = vegetarian_run
    basket_ids = {item.food.id for item in result.items}
    categories = categorize_unused(foods, result, quotes)
    not_selected = {food.id for food, _ in categories[UnusedCategory.NOT_SELECTED]}
    expected = {
        f.id for f in foods if f.id not in basket_ids and f.id not in result.excluded_foods
    }
    assert not_selected == expected
    for _, reason in categories[UnusedCategory.NOT_SELECTED]:
        assert f"${result.budget:.0f}" in reason


def test_pantry_used_food_is_never_listed_unused(foods, foods_by_id, seed_quotes, nutrition):
    # A pantry food the plan uses (but doesn't buy) must not appear in any
    # unused category — the plan does use it.
    profile = HouseholdProfile(adults=2, children=2, city="Los Angeles", zip_code="90001")
    demand = {"rice_white": 1500.0, "chicken_breast": 1200.0, "carrots": 700.0}
    result = result_from_demand(
        demand, profile, 50.0, 7, foods_by_id, seed_quotes, nutrition,
        pantry={"rice_white": 8000.0},  # fully stocked -> used, never bought
    )
    assert any(u.food.id == "rice_white" for u in result.pantry_used)
    assert all(item.food.id != "rice_white" for item in result.items)
    categories = categorize_unused(foods, result, seed_quotes)
    listed = {food.id for entries in categories.values() for food, _ in entries}
    assert "rice_white" not in listed


def test_categories_and_basket_partition_catalog(foods, vegetarian_run):
    result, quotes = vegetarian_run
    basket_ids = {item.food.id for item in result.items}
    categories = categorize_unused(foods, result, quotes)
    categorized: list[str] = []
    for entries in categories.values():
        categorized.extend(food.id for food, _ in entries)
    assert len(categorized) == len(set(categorized)), "a food landed in two categories"
    assert set(categorized) & basket_ids == set()
    assert set(categorized) | basket_ids == {f.id for f in foods}
