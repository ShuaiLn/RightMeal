"""Per-person daily calorie validation.

Slot targets are soft; the per-person daily total is the hard calorie
constraint (review). If a day is short, the fix is to add a verified side
recipe — never arbitrary rice, tortillas, oil, or loose ingredients.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.meals import DAILY_KCAL_TOLERANCE, DayPlan
from models.profile import HouseholdProfile


@dataclass(frozen=True)
class DailyCalorieResult:
    per_person_kcal: float
    target_per_person_kcal: float
    within_tolerance: bool
    shortfall_kcal: float   # per-person kcal still needed (0 if not short)
    surplus_kcal: float     # per-person kcal over the ceiling (0 if not over)


def evaluate_day(
    day: DayPlan, profile: HouseholdProfile, target_per_person_daily_kcal: float,
    tolerance: float = DAILY_KCAL_TOLERANCE,
) -> DailyCalorieResult:
    members = max(profile.total_members, 1)
    total = sum(meal.kcal for meal in day.meals)
    per_person = total / members
    lo = target_per_person_daily_kcal * (1 - tolerance)
    hi = target_per_person_daily_kcal * (1 + tolerance)
    return DailyCalorieResult(
        per_person_kcal=per_person,
        target_per_person_kcal=target_per_person_daily_kcal,
        within_tolerance=lo <= per_person <= hi,
        shortfall_kcal=max(0.0, lo - per_person),
        surplus_kcal=max(0.0, per_person - hi),
    )
