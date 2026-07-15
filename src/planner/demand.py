"""Ingredient demand of a finished meal plan.

Total grams the plan must source per food = sum of meal draws (main + side
folded into portions; batchable components counted at their batch multiple;
prepared-leftover meals contribute nothing). This is what the shopping basket
is built from, so side-recipe ingredients always reach the basket.
"""

from __future__ import annotations

from models.basket import NutrientGap
from models.food import Nutrients
from models.meals import MealPlan
from services.nutrition import NutritionService
from services.pantry_flow import meal_draw_grams


def ingredient_demand(plan: MealPlan) -> dict[str, float]:
    demand: dict[str, float] = {}
    for day in plan.days:
        for meal in day.meals:
            for food_id, grams in meal_draw_grams(meal).items():
                demand[food_id] = demand.get(food_id, 0.0) + grams
    return demand


def consumed_gaps(plan: MealPlan, targets: Nutrients) -> list[NutrientGap]:
    """Nutrient gaps of what is actually scheduled to be eaten."""
    return NutritionService.gaps(plan.consumed_totals, targets)
