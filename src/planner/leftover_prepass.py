"""Pre-pass: schedule available prepared leftovers as ready meals.

Runs BEFORE the optimizer. Each assigned leftover pins one lunch/dinner slot
with a zero-ingredient meal built from the leftover's per-food remaining
grams, and its nutrients are subtracted from the purchase demand
(``demand_offset``) so the basket stops re-buying what is already cooked.

Pure and deterministic: the store is only read here — a leftover's servings
are decremented when the meal is actually eaten, so regenerating a plan never
consumes anything. The SavedPlan records the reservations in
``leftovers_used``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from models.food import Food, Nutrients
from models.meals import SLOT_KCAL_SHARE, Meal, MealPlan, MealPortion, MealSlot
from models.prepared_leftover import EPSILON, STATUS_AVAILABLE, PreparedLeftover
from models.profile import HouseholdProfile
from planner.food_labels import COOKED_YIELD_FACTORS
from services.nutrition import NutritionService

logger = logging.getLogger(__name__)

# A leftover only earns a whole meal slot when it is substantial enough for
# one: portions are not a nutrition unit (half a bowl of broth must not
# replace dinner), so gate on the slot's kcal/protein targets instead.
MIN_SLOT_KCAL_COVERAGE = 0.6
MIN_SLOT_PROTEIN_COVERAGE = 0.5

# Leftovers go to lunch and dinner only, earliest first — matching the batch
# leftover convention (cooked dinners never become breakfast).
_PREPASS_SLOTS = (MealSlot.LUNCH, MealSlot.DINNER)


@dataclass(frozen=True)
class PreassignedMeal:
    day_index: int
    slot: MealSlot
    meal: Meal  # is_leftover=True, prepared_leftover_id set
    leftover_id: str
    servings_used: float


def household_slot_targets(
    nutrition: NutritionService, profile: HouseholdProfile
) -> dict[MealSlot, Nutrients]:
    """Per-slot household nutrition targets (daily targets × slot kcal share)."""
    daily = nutrition.household_daily_targets(profile)
    return {slot: daily.scaled(share) for slot, share in SLOT_KCAL_SHARE.items()}


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _build_meal(
    leftover: PreparedLeftover, slot: MealSlot, foods_by_id: dict[str, Food]
) -> tuple[Meal, float] | None:
    """The ready meal for one slot, scaled to at most one household serving.
    None when no portion resolves against the catalog (nutrition unknowable)."""
    if leftover.servings_remaining <= EPSILON:
        return None
    servings_used = min(leftover.servings_remaining, 1.0)
    scale = servings_used / leftover.servings_remaining
    portions = []
    for p in leftover.portions:
        food = foods_by_id.get(p.food_id)
        grams = p.remaining_grams * scale
        if food is None or grams <= EPSILON:
            continue
        factor = COOKED_YIELD_FACTORS.get(food.id)
        portions.append(
            MealPortion(food=food, grams=grams, cooked_grams=grams * factor if factor else None)
        )
    if not portions:
        return None
    meal = Meal(
        slot=slot,
        template_id=leftover.source_meal_template_id,
        name=f"Leftovers: {leftover.meal_name}",
        portions=tuple(portions),
        is_leftover=True,
        batch_id=None,
        prepared_leftover_id=leftover.id,
    )
    return meal, servings_used


def _meets_slot_gate(meal: Meal, target: Nutrients) -> bool:
    nutrients = meal.nutrients
    if target.calories_kcal > 0 and (
        nutrients.calories_kcal < MIN_SLOT_KCAL_COVERAGE * target.calories_kcal
    ):
        return False
    if target.protein_g > 0 and (
        nutrients.protein_g < MIN_SLOT_PROTEIN_COVERAGE * target.protein_g
    ):
        return False
    return True


def assign_prepared_leftovers(
    leftovers: Sequence[PreparedLeftover],
    horizon_days: int,
    start_date: date,
    foods_by_id: dict[str, Food],
    slot_targets: dict[MealSlot, Nutrients],
) -> list[PreassignedMeal]:
    """Deterministically pin leftovers to the earliest suitable slots.

    Ordering: soonest use-by first (eat before it spoils), then prepared date,
    then id. A slot is only valid inside
    ``max(start_date, prepared_at) <= slot_date <= use_by_date`` — a dish can
    neither be eaten before it was cooked nor scheduled past its suggested
    window. Records with unparseable dates are skipped with a warning, never
    defaulted to today.
    """
    dated: list[tuple[date, date, PreparedLeftover]] = []
    for leftover in leftovers:
        if leftover.status != STATUS_AVAILABLE or leftover.servings_remaining <= EPSILON:
            continue
        use_by = _parse_date(leftover.use_by_date)
        prepared = _parse_date(leftover.prepared_at)
        if use_by is None or prepared is None:
            logger.warning(
                "skipping prepared leftover %s: unparseable dates (%r, %r)",
                leftover.id, leftover.prepared_at, leftover.use_by_date,
            )
            continue
        if use_by < start_date:
            continue
        dated.append((use_by, prepared, leftover))
    dated.sort(key=lambda entry: (entry[0], entry[1], entry[2].id))

    assignments: list[PreassignedMeal] = []
    taken: set[tuple[int, MealSlot]] = set()
    for use_by, prepared, leftover in dated:
        first_day = max((prepared - start_date).days, 0)
        last_day = min((use_by - start_date).days, horizon_days - 1)
        placed = False
        for day in range(first_day, last_day + 1):
            for slot in _PREPASS_SLOTS:
                if (day, slot) in taken:
                    continue
                built = _build_meal(leftover, slot, foods_by_id)
                if built is None:
                    placed = True  # unresolvable record: skip without a slot
                    break
                meal, servings_used = built
                if not _meets_slot_gate(meal, slot_targets.get(slot, Nutrients())):
                    continue
                taken.add((day, slot))
                assignments.append(
                    PreassignedMeal(
                        day_index=day,
                        slot=slot,
                        meal=meal,
                        leftover_id=leftover.id,
                        servings_used=servings_used,
                    )
                )
                placed = True
                break
            if placed:
                break
    return assignments


def preassigned_nutrients(assignments: Sequence[PreassignedMeal]) -> Nutrients:
    """What the pinned ready meals already provide — the optimizer's
    ``demand_offset``, computed from remaining grams so an ingredient that is
    gone ('the chicken was finished') is never counted as covered."""
    total = Nutrients()
    for assignment in assignments:
        total = total.plus(assignment.meal.nutrients)
    return total


def prepared_meal_totals(meal_plan: MealPlan) -> Nutrients:
    """Nutrients contributed by scheduled prepared leftovers — derived, never
    stored. Everything else in consumed_totals draws raw ingredients."""
    total = Nutrients()
    for day in meal_plan.days:
        for meal in day.meals:
            if meal.prepared_leftover_id is not None:
                total = total.plus(meal.nutrients)
    return total


def pantry_used_totals(pantry_used: dict[str, float], foods_by_id: dict[str, Food]) -> Nutrients:
    """Nutrients of the pantry grams a plan counts on using (SavedPlan.pantry_used)."""
    total = Nutrients()
    for food_id, grams in pantry_used.items():
        food = foods_by_id.get(food_id)
        if food is not None:
            total = total.plus(food.nutrients_per_purchased_100g().scaled(grams / 100.0))
    return total


def basket_draw_totals(
    consumed: Nutrients, prepared: Nutrients, pantry_used: Nutrients
) -> Nutrients:
    """What scheduled meals draw from newly PURCHASED food:
    consumed − prepared-leftover meals − pantry-sourced grams, per nutrient.

    Prepared leftovers must never count against the shopping basket. Tiny
    float negatives clamp to zero; a materially negative value means the
    accounting is broken and is logged, never silently hidden.
    """
    values: dict[str, float] = {}
    for name in Nutrients.NAMES:
        value = consumed.get(name) - prepared.get(name) - pantry_used.get(name)
        if value < -max(1e-6 * abs(consumed.get(name)), 1e-3):
            logger.warning(
                "basket draw for %s is materially negative (%.4f) — totals accounting is off",
                name, value,
            )
        values[name] = max(value, 0.0)
    return Nutrients(**values)
