"""Recipe catalog integrity + the Phase-1 exit gate.

Guards the compiled recipe_index.json: it loads and validates, meal-type gating
is correct, auto-plannable recipes are genuinely complete, and the catalog can
supply a full week (enough breakfast and lunch/dinner candidates) using only
reviewed nutrition.
"""

from __future__ import annotations

import json
from pathlib import Path

from data.loader import load_catalog, load_recipe_index
from models.recipe import RecipeType

DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "data"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "src" / "assets" / "recipe_images"

NON_MEAL_TYPES = {
    RecipeType.SAUCE, RecipeType.SEASONING, RecipeType.DRINK,
    RecipeType.DESSERT, RecipeType.BASE,
}


def test_catalog_loads_and_every_food_id_resolves():
    catalog = {f.id for f in load_catalog()}
    recipes = load_recipe_index()
    assert len(recipes) > 300
    for r in recipes:
        for ing in r.ingredients:
            if ing.canonical_food_id is not None:
                assert ing.canonical_food_id in catalog, (r.id, ing.canonical_food_id)


def test_non_meal_types_never_fill_a_slot():
    for r in load_recipe_index():
        if r.recipe_type in NON_MEAL_TYPES:
            assert r.meal_types == (), (r.id, r.recipe_type)


def test_auto_plannable_recipes_are_complete():
    for r in load_recipe_index():
        if not r.auto_plannable:
            continue
        assert r.meal_types, r.id
        assert r.core_coverage >= 1.0, (r.id, r.core_coverage)
        assert r.coverage_by_mass >= 0.90, (r.id, r.coverage_by_mass)
        assert r.nutrition_per_serving.calories_kcal > 0, r.id
        # No cooked quantity computed on dry-food nutrients: every resolved
        # ingredient carries an explicit quantity_state.
        for ing in r.ingredients:
            assert ing.quantity_state in ("raw", "dry", "cooked", "drained", "canned")


def test_phase1_exit_gate_candidate_counts():
    recipes = load_recipe_index()
    plannable = [r for r in recipes if r.auto_plannable]
    breakfast = sum(1 for r in plannable if "breakfast" in r.meal_types)
    lunch = sum(1 for r in plannable if "lunch" in r.meal_types)
    dinner = sum(1 for r in plannable if "dinner" in r.meal_types)
    assert breakfast >= 21, f"breakfast candidates {breakfast} < 21"
    assert lunch >= 35, f"lunch candidates {lunch} < 35"
    assert dinner >= 35, f"dinner candidates {dinner} < 35"


def test_a_full_week_of_distinct_candidates_exists():
    """High Variety needs 7 distinct recipes per slot for a 7-day plan."""
    recipes = load_recipe_index()
    for slot in ("breakfast", "lunch", "dinner"):
        distinct = {r.id for r in recipes if r.auto_plannable and slot in r.meal_types}
        assert len(distinct) >= 7, (slot, len(distinct))


def test_extended_foods_nutrition_is_reviewed():
    """No unreviewed USDA data in the production catalog: every extended food
    has a reviewed mapping."""
    mappings = json.loads((DATA_DIR / "usda_food_mappings.json").read_text(encoding="utf-8"))["mappings"]
    extended = json.loads((DATA_DIR / "extended_foods.json").read_text(encoding="utf-8"))["foods"]
    for food in extended:
        rec = mappings.get(food["id"])
        assert rec is not None and rec.get("reviewed") is True, food["id"]


def test_most_referenced_dish_images_are_present():
    recipes = load_recipe_index()
    referenced = [r.image_asset for r in recipes if r.image_asset]
    present = sum(1 for a in referenced if (ASSETS_DIR / Path(a).name).exists())
    # Origin occasionally 404s a few; the app falls back to a placeholder.
    assert present >= len(referenced) - 5, (present, len(referenced))
