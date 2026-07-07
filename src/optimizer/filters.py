"""Hard dietary exclusions. Allergies and diet rules are never traded off."""

from __future__ import annotations

from typing import Sequence

from models.food import Food
from models.profile import HouseholdProfile


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
