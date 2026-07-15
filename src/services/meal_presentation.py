"""Pure helpers for choosing what photo represents a meal.

Domain logic, not UI, so both ``ui/meals_section.py`` and ``ui/calendar_view.py``
can depend on it without any UI-to-UI coupling.
"""

from __future__ import annotations

from models.food import Food
from models.meals import Meal


def representative_food_for_meal(meal: Meal) -> Food | None:
    """The most visually representative ingredient for a meal's thumbnail:
    the largest solid (non-liquid) portion by grams. Skips oils/broths/other
    liquids, which don't read as "a photo of the dish" — confirmed via
    Food.is_liquid, already a required field (models/food.py)."""
    solids = [p for p in meal.portions if not p.food.is_liquid]
    pool = solids or list(meal.portions)
    if not pool:
        return None
    return max(pool, key=lambda p: p.grams).food
