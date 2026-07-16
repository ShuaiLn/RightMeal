"""Focused presentation checks for explicit partial-plan portion semantics."""

from models import Meal, MealPortion, MealSlot
from ui.meals_section import (
    PARTIAL_FOOD_COVERAGE_WARNING,
    STALE_PLAN_WARNING,
    meal_per_person_nutrition_label,
    meal_serving_summary,
    partial_plan_warning_banner,
    stale_plan_warning_banner,
)


def _partial_meal(food) -> Meal:
    return Meal(
        slot=MealSlot.DINNER,
        template_id="",
        name="Reduced dinner",
        portions=(MealPortion(food, 300.0),),
        servings=3.0,
        household_member_count=5,
        full_serving_equivalent=3.0,
        portion_scale=0.60,
    )


def test_meal_copy_distinguishes_people_from_full_serving_equivalents(foods_by_id):
    meal = _partial_meal(foods_by_id["rice_white"])

    assert meal_serving_summary(meal) == (
        "Household: 5 people · 3 full-serving equivalents · 60% portions"
    )
    label = meal_per_person_nutrition_label(meal)
    assert label.startswith("Per person (estimated): ≈")
    assert f"{meal.kcal / 5:,.0f} kcal" in label
    assert f"{meal.nutrients.protein_g / 5:,.1f} g protein" in label


def test_warning_banners_keep_required_copy_visible():
    partial = partial_plan_warning_banner()
    stale = stale_plan_warning_banner()

    assert PARTIAL_FOOD_COVERAGE_WARNING == (
        "This is not a complete food plan. Additional food is required."
    )
    assert partial.content.controls[1].value == PARTIAL_FOOD_COVERAGE_WARNING
    assert stale.content.controls[1].value == STALE_PLAN_WARNING
