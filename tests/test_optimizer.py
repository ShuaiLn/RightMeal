"""Optimizer acceptance tests, including the LA $50/week family case."""

import pytest

from models import FoodGroup, HouseholdProfile
from optimizer import optimize
from services.nutrition import NutritionService


@pytest.fixture
def la_result(foods, seed_quotes, la_family_profile, nutrition):
    return optimize(
        foods=foods,
        quotes=seed_quotes,
        profile=la_family_profile,
        budget=50.0,
        horizon_days=7,
        nutrition=nutrition,
    )


class TestLaFamilyCase:
    """Los Angeles, $50/week, 2 adults + 2 children, no restrictions."""

    def test_stays_within_budget(self, la_result):
        assert la_result.budget_feasible
        assert 0 < la_result.total_cost <= 50.0
        assert sum(item.cost for item in la_result.items) == pytest.approx(
            la_result.total_cost, abs=0.01
        )

    def test_covers_at_least_five_food_groups(self, la_result):
        assert la_result.groups_covered >= 5
        assert len(la_result.group_coverage) == la_result.groups_covered

    def test_family_basket_has_at_least_seven_distinct_foods(self, la_result):
        assert la_result.distinct_foods >= 7
        assert len({item.food.id for item in la_result.items}) == la_result.distinct_foods

    def test_missing_nutrients_are_reported(self, la_result):
        # $50/week for four people cannot meet every DRI-based target;
        # the result must say so instead of hiding it.
        assert la_result.gaps
        for gap in la_result.gaps:
            assert gap.pct < 100.0
            assert gap.target > 0

    def test_group_coverage_is_in_the_result(self, la_result):
        assert isinstance(la_result.group_coverage, dict)
        for group, grams in la_result.group_coverage.items():
            assert isinstance(group, FoodGroup)
            assert grams > 0

    def test_deterministic_across_runs(self, foods, seed_quotes, la_family_profile, nutrition):
        results = [
            optimize(foods, seed_quotes, la_family_profile, 50.0, 7, nutrition)
            for _ in range(2)
        ]
        assert results[0].items == results[1].items
        assert results[0].score == results[1].score
        assert results[0].total_cost == results[1].total_cost


class TestCaps:
    def test_per_food_gram_caps_respected(self, la_result, la_family_profile):
        members = la_family_profile.total_members
        for item in la_result.items:
            cap = max(item.food.max_weekly_grams * members, item.food.smallest_package.grams)
            assert item.grams <= cap + 1e-6, item.food.id

    def test_per_group_gram_caps_respected(self, la_result, nutrition, la_family_profile):
        caps = nutrition.group_caps_g(la_family_profile, horizon_days=7)
        for group, grams in la_result.group_coverage.items():
            assert grams <= caps[group] + 1e-6, group

    def test_no_single_cheap_item_dominates_at_50(self, la_result):
        # With a workable budget the dominance constraint should hold.
        assert not la_result.dominance_flags

    def test_single_person_three_day_plan_uses_small_milk(
        self, foods, seed_quotes, nutrition
    ):
        profile = HouseholdProfile(adults=1, city="Los Angeles", zip_code="90001")
        result = optimize(foods, seed_quotes, profile, 20.0, 3, nutrition)
        assert result.budget_feasible
        # Milk cap for 1 person x 3 days is ~1714 g: a 1-gallon jug (3899 g)
        # can never fit, so any milk in the basket must be a smaller package.
        for item in result.items:
            if item.food.id == "milk_whole":
                assert item.package.label != "1 gallon"
            cap = max(
                item.food.max_weekly_grams * 1 * 3 / 7, item.food.smallest_package.grams
            )
            assert item.grams <= cap + 1e-6, item.food.id


class TestBudgetScaling:
    def test_higher_budget_scores_at_least_as_well(
        self, foods, seed_quotes, la_family_profile, nutrition
    ):
        low = optimize(foods, seed_quotes, la_family_profile, 30.0, 7, nutrition)
        high = optimize(foods, seed_quotes, la_family_profile, 120.0, 7, nutrition)
        assert high.score >= low.score
        assert high.total_cost <= 120.0

    def test_horizon_scales_budget_usage(self, foods, seed_quotes, la_family_profile, nutrition):
        result = optimize(foods, seed_quotes, la_family_profile, 21.0, 3, nutrition)
        assert result.total_cost <= 21.0
        assert result.budget_feasible
