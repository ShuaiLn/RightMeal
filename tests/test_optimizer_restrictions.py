"""Hard exclusions and low-budget honesty tests."""

from models import HouseholdProfile
from optimizer import optimize


def run(foods, seed_quotes, nutrition, profile, budget=50.0, horizon_days=7):
    return optimize(foods, seed_quotes, profile, budget, horizon_days, nutrition)


class TestAllergies:
    def test_peanut_allergy_excludes_peanut_butter(self, foods, seed_quotes, nutrition):
        profile = HouseholdProfile(adults=2, children=2, allergies=["peanut"], zip_code="90001")
        result = run(foods, seed_quotes, nutrition, profile)
        assert all(item.food.id != "peanut_butter" for item in result.items)
        assert "peanut" in result.excluded_foods["peanut_butter"]

    def test_allergy_excluded_even_at_very_low_budget(self, foods, seed_quotes, nutrition):
        """Cheap and nutritious never overrides an allergy."""
        profile = HouseholdProfile(adults=1, allergies=["peanut"], zip_code="90001")
        result = run(foods, seed_quotes, nutrition, profile, budget=8.0)
        assert all(item.food.id != "peanut_butter" for item in result.items)

    def test_dairy_allergy_leaves_fortified_alternative(self, foods, seed_quotes, nutrition):
        profile = HouseholdProfile(adults=2, children=2, allergies=["dairy"], zip_code="90001")
        result = run(foods, seed_quotes, nutrition, profile)
        basket_ids = {item.food.id for item in result.items}
        assert not basket_ids & {"milk_whole", "yogurt_plain", "cheddar_cheese"}
        assert "milk_whole" in result.excluded_foods

    def test_egg_allergy(self, foods, seed_quotes, nutrition):
        profile = HouseholdProfile(adults=2, allergies=["egg"], zip_code="90001")
        result = run(foods, seed_quotes, nutrition, profile)
        assert all(item.food.id != "eggs_large" for item in result.items)


class TestDietModes:
    def test_vegetarian_excludes_meat_and_fish(self, foods, seed_quotes, nutrition):
        profile = HouseholdProfile(adults=2, children=2, vegetarian=True, zip_code="90001")
        result = run(foods, seed_quotes, nutrition, profile)
        assert all(not item.food.is_meat_or_fish for item in result.items)
        assert result.excluded_foods["chicken_breast"] == "not vegetarian"
        assert result.groups_covered >= 5  # protein group still coverable

    def test_lactose_free_uses_lactose_free_dairy_group(self, foods, seed_quotes, nutrition):
        profile = HouseholdProfile(adults=2, children=2, lactose_free=True, zip_code="90001")
        result = run(foods, seed_quotes, nutrition, profile)
        assert all(not item.food.lactose for item in result.items)
        assert result.excluded_foods["milk_whole"] == "contains lactose"
        # cheddar (aged) and fortified soy milk stay available
        assert "cheddar_cheese" not in result.excluded_foods
        assert "soy_milk_fortified" not in result.excluded_foods


class TestLowBudgetHonesty:
    def test_12_dollar_family_week_is_honest(self, foods, seed_quotes, nutrition, la_family_profile):
        result = run(foods, seed_quotes, nutrition, la_family_profile, budget=12.0)
        assert result.budget_feasible  # something affordable exists
        assert result.total_cost <= 12.0
        assert not result.nutrition_feasible  # but targets cannot be met
        assert result.relaxed_constraints  # ...and the result says so
        assert result.gaps
        assert result.penalties_applied  # explicit penalties, not silent failure

    def test_impossible_budget_is_reported_not_crashed(
        self, foods, seed_quotes, nutrition, la_family_profile
    ):
        result = run(foods, seed_quotes, nutrition, la_family_profile, budget=0.25)
        assert not result.budget_feasible
        assert not result.nutrition_feasible
        assert result.items == ()
        assert result.total_cost == 0
        assert any("too low" in c for c in result.relaxed_constraints)

    def test_low_budget_never_exceeds_budget(self, foods, seed_quotes, nutrition):
        profile = HouseholdProfile(adults=1, zip_code="90001")
        for budget in (2.0, 5.0, 8.0, 15.0):
            result = run(foods, seed_quotes, nutrition, profile, budget=budget)
            assert result.total_cost <= budget + 1e-9
