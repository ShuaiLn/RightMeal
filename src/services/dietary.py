"""Hard dietary exclusions for foods and recipes.

Allergies and diet rules are never traded off — they exclude, they never just
lower a score. This is the single gate used by both the shopping basket builder
and the recipe-first meal validator.
"""

from __future__ import annotations

from typing import Sequence, TYPE_CHECKING

from models.food import Food
from models.profile import HouseholdProfile

if TYPE_CHECKING:
    from models.recipe import Recipe


def exclusion_reason(food: Food, profile: HouseholdProfile) -> str | None:
    for allergen in profile.allergies:
        if allergen.strip().lower() in food.allergen_tags:
            return f"contains allergen '{allergen.strip().lower()}'"
    if profile.vegetarian and not food.vegetarian:
        return "not vegetarian"
    if profile.no_pork and food.contains_pork:
        return "contains pork"
    if profile.lactose_free and food.lactose:
        return "contains lactose"
    return None


def apply_exclusions(
    foods: Sequence[Food], profile: HouseholdProfile
) -> tuple[list[Food], dict[str, str]]:
    """Split foods into (allowed, {excluded_food_id: reason})."""
    allowed: list[Food] = []
    excluded: dict[str, str] = {}
    for food in foods:
        reason = exclusion_reason(food, profile)
        if reason is None:
            allowed.append(food)
        else:
            excluded[food.id] = reason
    return allowed, excluded


def recipe_exclusion_reason(
    recipe: "Recipe", profile: HouseholdProfile, foods_by_id: dict[str, Food]
) -> str | None:
    """First dietary conflict for a recipe, or None if it fits the profile.

    Checks the recipe's derived flags first (cheap), then every resolved
    ingredient food against the same food-level rules the basket uses, so a
    recipe can never slip a violating ingredient past the meal validator.
    """
    if profile.vegetarian and recipe.is_meat_or_fish:
        return "not vegetarian"
    if profile.no_pork and recipe.contains_pork:
        return "contains pork"
    for allergen in profile.allergies:
        if allergen.strip().lower() in recipe.allergen_tags:
            return f"contains allergen '{allergen.strip().lower()}'"
    for ing in recipe.ingredients:
        if ing.is_nonfood:
            continue
        food = foods_by_id.get(ing.canonical_food_id) if ing.canonical_food_id else None
        if food is None:
            continue
        reason = exclusion_reason(food, profile)
        if reason is not None:
            return f"{food.name}: {reason}"
    return None
