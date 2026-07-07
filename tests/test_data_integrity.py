"""Integrity checks over the curated JSON data files."""

from data import load_bls_price_map, load_nutrient_targets, load_seed_foods
from models import FoodGroup, Nutrients


def test_seed_foods_load_and_validate():
    foods = load_seed_foods()
    assert len(foods) >= 25


def test_all_six_food_groups_covered():
    foods = load_seed_foods()
    assert {f.food_group for f in foods} == set(FoodGroup)


def test_every_food_has_packages_and_positive_nutrition():
    for food in load_seed_foods():
        assert food.package_options, food.id
        assert all(p.grams > 0 and p.seed_price > 0 for p in food.package_options), food.id
        assert food.nutrients_per_100g.calories_kcal > 0, food.id


def test_small_package_foods_have_multiple_sizes():
    foods = {f.id: f for f in load_seed_foods()}
    for food_id in ("milk_whole", "eggs_large", "rice_white"):
        assert len(foods[food_id].package_options) >= 2, food_id


def test_liquids_have_density_and_ml():
    for food in load_seed_foods():
        if food.is_liquid:
            assert food.density_g_per_ml and food.density_g_per_ml > 0, food.id
            assert all(p.ml for p in food.package_options), food.id


def test_eggs_use_spec_conversions():
    foods = {f.id: f for f in load_seed_foods()}
    dozen = next(p for p in foods["eggs_large"].package_options if p.label == "1 dozen")
    assert dozen.grams == 600


def test_peanut_butter_tagged_for_allergy_exclusion():
    foods = {f.id: f for f in load_seed_foods()}
    assert "peanut" in foods["peanut_butter"].allergen_tags


def test_dairy_and_lactose_flags():
    foods = {f.id: f for f in load_seed_foods()}
    assert foods["milk_whole"].lactose is True
    assert foods["cheddar_cheese"].lactose is False  # aged cheese, but still a dairy allergen
    assert "dairy" in foods["cheddar_cheese"].allergen_tags
    assert foods["soy_milk_fortified"].lactose is False
    assert foods["soy_milk_fortified"].food_group is FoodGroup.DAIRY_FORTIFIED_ALT


def test_meat_flags_consistent_with_vegetarian():
    for food in load_seed_foods():
        if food.is_meat_or_fish:
            assert not food.vegetarian, food.id
        if food.vegan:
            assert food.vegetarian, food.id


def test_bls_map_only_references_known_foods():
    bls = load_bls_price_map()
    food_ids = {f.id for f in load_seed_foods()}
    assert set(bls["series"]).issubset(food_ids)


def test_bls_deliberately_unmapped_foods():
    """Oats, lentils, and frozen vegetables must never gain a BLS mapping by accident."""
    series = load_bls_price_map()["series"]
    for food_id in ("rolled_oats", "lentils_dry", "broccoli_frozen", "mixed_veg_frozen"):
        assert series.get(food_id) is None, food_id


def test_nutrient_targets_complete():
    targets = load_nutrient_targets()
    for person_type in ("adult", "child", "senior"):
        assert set(Nutrients.NAMES).issubset(targets["person_types"][person_type])
    assert set(targets["group_weekly_caps_g_per_person"]) == {g.value for g in FoodGroup}
