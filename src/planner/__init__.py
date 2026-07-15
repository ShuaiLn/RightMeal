"""Meal planning layer: the recipe-first engine and its supporting utilities."""

from planner.demand import consumed_gaps, ingredient_demand
from planner.food_labels import COOKED_YIELD_FACTORS, SHORT_NAMES
from planner.leftover_prepass import (
    PreassignedMeal,
    assign_prepared_leftovers,
    basket_draw_totals,
    household_slot_targets,
    pantry_used_totals,
    preassigned_nutrients,
    prepared_meal_totals,
)
from planner.unused import UnusedCategory, categorize_unused

__all__ = [
    "COOKED_YIELD_FACTORS",
    "PreassignedMeal",
    "SHORT_NAMES",
    "UnusedCategory",
    "assign_prepared_leftovers",
    "basket_draw_totals",
    "categorize_unused",
    "consumed_gaps",
    "household_slot_targets",
    "ingredient_demand",
    "pantry_used_totals",
    "preassigned_nutrients",
    "prepared_meal_totals",
]
