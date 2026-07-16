"""Explicit nonfood classification and its food-planning invariants."""

# The indexer is a script package added to ``sys.path`` below.
# ruff: noqa: E402

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from recipe_indexer.ingredient_parser import parse_ingredient_line
from recipe_indexer.nonfood import is_nonfood_ingredient
from recipe_indexer.nutrition import compute
from recipe_indexer.resolver import Resolution
from recipe_indexer.roles import ResolvedIngredient, classify_ingredient

from models.recipe import RecipeIngredient
from data.loader import load_recipe_index
from planner.recipe_scheduler import _portions_for_recipe, _rough_recipe_cost_hint
from services.planner_engine import collect_recipe_data_issues


@pytest.mark.parametrize(
    "line",
    (
        "1 cheesecloth",
        "2 thermos, 1/2 l capacity each",
        "6 baking paper square 7x7cm",
        "parchment paper",
        "Tin foil",
        "Aluminum Foil",
        "12 wooden skewers",
        "toothpicks",
        "3 resealable containers",
        "find sieve or cheese cloth",
        "Fine mesh strainer",
        "Butchers twine",
        "5l (1 gallon) jar",
        "Wood chips (optional, fuel for smoky flavor)",
    ),
)
def test_clear_equipment_and_material_lines_are_nonfood(line):
    parsed = parse_ingredient_line(line)
    assert is_nonfood_ingredient(line, parsed.name)


@pytest.mark.parametrize(
    "line",
    (
        "1 container of barbecue rub",
        "a jar of apple sauce",
        "24 oz or 1 jar of tomato sauce",
        "1 Sheet of Dried Nori (Optional)",
        "2 tins sardines",
        "Red Paper Flakes",
        "chicken skewers",
        "wood ear mushrooms",
        "For the Baking Tin: More Butter and Flour",
        "12 garlic gloves",
    ),
)
def test_ambiguous_or_packaged_food_lines_are_not_nonfood(line):
    parsed = parse_ingredient_line(line)
    assert not is_nonfood_ingredient(line, parsed.name)


def test_nonfood_classification_is_inert_and_preserves_optional():
    parsed = parse_ingredient_line("6 baking paper squares (optional)")
    resolved = classify_ingredient(
        parsed,
        # Even an erroneous resolver hit cannot turn equipment into food.
        Resolution("rice_white", "exact", 1.0),
        {"rice_white": {"role": "main_carb", "default_state": "dry"}},
        servings=2,
        portion_defaults={"count_grams": {"_default": {"_piece": 100}}},
    )

    assert resolved.is_nonfood is True
    assert resolved.optional is True
    assert resolved.is_seasoning is False
    assert resolved.food_id is None
    assert resolved.nutrition_basis is None
    assert resolved.grams_per_serving is None
    assert resolved.is_core is False
    assert resolved.role == "nonfood"
    assert resolved.match_method == "nonfood"


def test_nonfood_does_not_affect_nutrition_mass_or_core_coverage():
    food = ResolvedIngredient(
        raw_text="50 g rice",
        food_id="rice",
        role="main_carb",
        parent_category="rice",
        quantity_state="dry",
        nutrition_basis="rice",
        grams_per_serving=50.0,
        is_core=True,
        is_seasoning=False,
        match_method="exact",
        confidence=1.0,
        optional=False,
        is_nonfood=False,
    )
    # Deliberately populate forbidden food fields: the explicit flag remains
    # authoritative for all nutrition/core accounting.
    equipment = ResolvedIngredient(
        raw_text="100 g parchment paper",
        food_id="rice",
        role="main_carb",
        parent_category="rice",
        quantity_state="dry",
        nutrition_basis="rice",
        grams_per_serving=100.0,
        is_core=True,
        is_seasoning=False,
        match_method="bad-test-input",
        confidence=1.0,
        optional=False,
        is_nonfood=True,
    )

    result = compute(
        [food, equipment],
        {"rice": {"calories_kcal": 200.0, "protein_g": 10.0}},
        meal_types_nonempty=True,
    )

    assert result.coverage_by_mass == 1.0
    assert result.core_coverage == 1.0
    assert result.unresolved_core_texts == ()
    assert result.nutrition_per_serving["calories_kcal"] == 100.0
    assert result.nutrition_per_serving["protein_g"] == 5.0


def test_recipe_ingredient_persists_signal_and_legacy_defaults_false():
    legacy = RecipeIngredient.from_dict({"raw_text": "1 apple"})
    equipment = RecipeIngredient.from_dict(
        {"raw_text": "parchment paper", "is_nonfood": True}
    )

    assert legacy.is_nonfood is False
    assert equipment.is_nonfood is True


def test_nonfood_is_authoritative_for_runtime_demand_cost_and_data_quality(
    foods_by_id, la_family_profile
):
    equipment = RecipeIngredient.from_dict(
        {
            "raw_text": "100 g parchment paper",
            # Deliberately corrupt food-like fields prove that consumers use
            # is_nonfood as the authoritative exclusion signal.
            "canonical_food_id": "rice_white",
            "normalized_id": "rice_white",
            "role": "main_carb",
            "grams_per_serving": 100,
            "is_core": True,
            "is_nonfood": True,
        }
    )
    base = next(recipe for recipe in load_recipe_index() if recipe.id == "yogurt")
    recipe = replace(base, ingredients=(equipment,))

    assert recipe.core_ingredient_ids == frozenset()
    assert _portions_for_recipe(recipe, foods_by_id, members=2, component="main") == []
    assert _rough_recipe_cost_hint(recipe, members=2, quotes={"rice_white": object()}) == 0.0
    assert collect_recipe_data_issues(
        (recipe,), foods_by_id, la_family_profile, pricing={}
    ) == ()
