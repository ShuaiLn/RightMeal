"""Weighted recipe similarity for variety control.

Not a plain Jaccard-plus-shared-cooking-method rule (review): main protein,
main carbohydrate, dish category, and core ingredients matter much more than a
generic method like "baked" or "boiled", so two different baked dishes are not
treated as the same meal.
"""

from __future__ import annotations

from models.recipe import Recipe

# Contribution weights; scores are normalized to [0, 1] by the total weight.
_W_SAME_RECIPE = 1.0     # identical canonical recipe -> maximally similar
_W_PROTEIN = 0.30
_W_CARB = 0.25
_W_CATEGORY = 0.20
_W_CORE_INGREDIENTS = 0.15
_W_CUISINE = 0.06
_W_METHOD = 0.04

DEFAULT_SIMILARITY_THRESHOLD = 0.65


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def similarity_score(a: Recipe, b: Recipe) -> float:
    if a.id == b.id:
        return 1.0
    score = 0.0
    if a.main_protein and a.main_protein == b.main_protein:
        score += _W_PROTEIN
    if a.main_carbs and b.main_carbs and set(a.main_carbs) & set(b.main_carbs):
        score += _W_CARB
    if a.dish_category == b.dish_category:
        score += _W_CATEGORY
    score += _W_CORE_INGREDIENTS * _jaccard(a.core_ingredient_ids, b.core_ingredient_ids)
    if a.cuisine == b.cuisine and a.cuisine != "international":
        score += _W_CUISINE
    if set(a.cooking_methods) & set(b.cooking_methods):
        score += _W_METHOD
    total = (_W_PROTEIN + _W_CARB + _W_CATEGORY + _W_CORE_INGREDIENTS
             + _W_CUISINE + _W_METHOD)
    return min(score / total, 1.0)


def is_similar(a: Recipe, b: Recipe, threshold: float = DEFAULT_SIMILARITY_THRESHOLD) -> bool:
    return similarity_score(a, b) >= threshold


def identical_core_structure(a: Recipe, b: Recipe) -> bool:
    """Same protein + carb + dish category — used to forbid the same core
    structure in two consecutive meals regardless of overall similarity."""
    return (
        a.main_protein == b.main_protein
        and set(a.main_carbs) == set(b.main_carbs)
        and a.dish_category == b.dish_category
    )
