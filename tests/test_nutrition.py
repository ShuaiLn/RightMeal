"""Nutrition service tests: household targets, horizon scaling, gaps, coverage."""

import pytest

from models import BasketItem, FoodGroup, HouseholdProfile, Nutrients, PriceQuote, PriceSource


def seed_quote(food):
    pkg = food.smallest_package
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


def test_household_weekly_targets_sum_members(nutrition, la_family_profile):
    weekly = nutrition.household_targets(la_family_profile, horizon_days=7)
    # 2 adults (2200) + 2 children (1600) = 7600 kcal/day -> x7
    assert weekly.calories_kcal == pytest.approx(7600 * 7)
    assert weekly.protein_g == pytest.approx((2 * 55 + 2 * 25) * 7)


def test_horizon_scales_targets(nutrition, la_family_profile):
    three_day = nutrition.household_targets(la_family_profile, horizon_days=3)
    weekly = nutrition.household_targets(la_family_profile, horizon_days=7)
    assert three_day.calories_kcal == pytest.approx(weekly.calories_kcal * 3 / 7)


def test_senior_targets_used(nutrition):
    profile = HouseholdProfile(adults=0, seniors=1)
    weekly = nutrition.household_targets(profile, horizon_days=7)
    assert weekly.calories_kcal == pytest.approx(1900 * 7)
    assert weekly.vitamin_d_mcg == pytest.approx(20 * 7)


def test_basket_totals_and_edible_fraction(nutrition, foods_by_id):
    bananas = foods_by_id["bananas"]
    item = BasketItem(food=bananas, package=bananas.package_options[0], count=1, quote=seed_quote(bananas))
    totals = nutrition.basket_totals([item])
    # 453.6 g purchased x 0.64 edible x 89 kcal/100 g
    assert totals.calories_kcal == pytest.approx(453.6 * 0.64 * 0.89, rel=1e-3)


def test_gaps_only_below_target(nutrition):
    targets = Nutrients(calories_kcal=1000, protein_g=100, iron_mg=10)
    totals = Nutrients(calories_kcal=1200, protein_g=50, iron_mg=10)
    gaps = nutrition.gaps(totals, targets)
    gap_names = {g.nutrient for g in gaps}
    assert "protein_g" in gap_names
    assert "calories_kcal" not in gap_names
    assert "iron_mg" not in gap_names  # exactly met
    protein_gap = next(g for g in gaps if g.nutrient == "protein_g")
    assert protein_gap.pct == pytest.approx(50.0)


def test_group_coverage_sums_grams(nutrition, foods_by_id):
    rice = foods_by_id["rice_white"]
    milk = foods_by_id["milk_whole"]
    items = [
        BasketItem(food=rice, package=rice.package_options[0], count=2, quote=seed_quote(rice)),
        BasketItem(food=milk, package=milk.package_options[0], count=1, quote=seed_quote(milk)),
    ]
    coverage = nutrition.group_coverage(items)
    assert coverage[FoodGroup.GRAINS_STARCHY] == pytest.approx(2 * 453.6)
    assert coverage[FoodGroup.DAIRY_FORTIFIED_ALT] == pytest.approx(3899)
    assert FoodGroup.FRUITS not in coverage


def test_group_caps_scale_with_household_and_horizon(nutrition, la_family_profile):
    caps_week = nutrition.group_caps_g(la_family_profile, horizon_days=7)
    caps_3day = nutrition.group_caps_g(la_family_profile, horizon_days=3)
    assert caps_week[FoodGroup.GRAINS_STARCHY] == pytest.approx(3200 * 4)
    assert caps_3day[FoodGroup.GRAINS_STARCHY] == pytest.approx(3200 * 4 * 3 / 7)
