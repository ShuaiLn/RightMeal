"""Hard validation gate for a generated meal.

A candidate that fails ANY check here is rejected and another recipe is chosen —
it is never committed, persisted, or shown. Slot-calorie fit is intentionally
NOT here: it is a soft scoring signal; the hard calorie constraint is the
per-person daily total in ``daily_validator``.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.food import Food
from models.meals import Meal, MealSlot, SOURCE_RECIPE
from models.profile import HouseholdProfile
from models.recipe import Recipe, RecipeType
from services.dietary import recipe_exclusion_reason

# A per-person amount this many times the role's max (or below this fraction of
# its min) is a severe anomaly and hard-rejects the candidate.
_SEVERE_OVER = 2.5
_SEVERE_UNDER = 0.25

_PRIMARY_TYPES = {RecipeType.MAIN_MEAL, RecipeType.BREAKFAST}


@dataclass(frozen=True)
class PlanContext:
    profile: HouseholdProfile
    slot: MealSlot
    day_index: int
    per_person_daily_kcal: float
    portion_rules: dict
    recipes_by_id: dict[str, Recipe]
    foods_by_id: dict[str, Food]


def _members(profile: HouseholdProfile) -> int:
    return max(profile.total_members, 1)


def validate_meal(meal: Meal, ctx: PlanContext) -> list[str]:
    """Return hard-failure reasons; an empty list means the meal is valid."""
    reasons: list[str] = []

    # Provenance: a recipe-first meal must reference a real catalog recipe.
    if meal.source_kind != SOURCE_RECIPE or not meal.recipe_id:
        return ["not a recipe-first meal"]
    recipe = ctx.recipes_by_id.get(meal.recipe_id)
    if recipe is None:
        return [f"recipe {meal.recipe_id!r} not in catalog"]

    side = ctx.recipes_by_id.get(meal.side_recipe_id) if meal.side_recipe_id else None

    # Meal type / slot eligibility.
    if recipe.recipe_type not in _PRIMARY_TYPES:
        reasons.append(f"recipe_type {recipe.recipe_type.value} cannot be a {ctx.slot.value}")
    if ctx.slot.value not in recipe.meal_types:
        reasons.append(f"{recipe.id} not eligible for {ctx.slot.value}")

    # Name consistency: the shown name must come from the recipe (or a renamed
    # substitution), never a mechanical ingredient concatenation.
    if recipe.canonical_name.lower() not in meal.name.lower() and not meal.side_recipe_id:
        # Substitution renames replace a token in the canonical name, so the
        # canonical stem should still be recognizable; be lenient only for sides.
        if not any(sub.rename_replace and sub.rename_replace.lower() in meal.name.lower()
                   for sub in recipe.substitutions):
            reasons.append(f"name {meal.name!r} not derived from recipe {recipe.id!r}")

    # Single main carb unless the recipe explicitly allows more.
    carb_ids = {p.food.id for p in meal.portions
                if _role_of(p.food.id, recipe, side) == "main_carb"}
    if len(carb_ids) > 1 and not recipe.allow_multiple_main_carbs:
        reasons.append(f"{len(carb_ids)} main carbs but allow_multiple_main_carbs is false")

    # Portion foods must belong to the recipe (or the side, or a substitution target).
    allowed_ids = _allowed_food_ids(recipe, side)
    for p in meal.portions:
        if p.food.id not in allowed_ids:
            reasons.append(f"portion {p.food.id!r} not in recipe/side/substitutions")

    # Servings must be set and cover the household.
    if meal.servings <= 0:
        reasons.append("servings not set")
    elif abs(meal.servings - _members(ctx.profile)) > 0.5:
        reasons.append(f"servings {meal.servings} != household {_members(ctx.profile)}")

    # Per-person portion sanity (severe anomalies only; slot kcal is soft).
    reasons.extend(_portion_anomalies(meal, recipe, side, ctx))

    # Dietary / allergen gate.
    reason = recipe_exclusion_reason(recipe, ctx.profile, ctx.foods_by_id)
    if reason:
        reasons.append(f"dietary: {reason}")
    if side is not None:
        sreason = recipe_exclusion_reason(side, ctx.profile, ctx.foods_by_id)
        if sreason:
            reasons.append(f"side dietary: {sreason}")

    return reasons


def _role_of(food_id: str, recipe: Recipe, side: Recipe | None) -> str | None:
    for r in (recipe, side):
        if r is None:
            continue
        for ing in r.ingredients:
            if ing.canonical_food_id == food_id:
                return ing.role
    return None


def _allowed_food_ids(recipe: Recipe, side: Recipe | None) -> set[str]:
    ids: set[str] = set()
    for r in (recipe, side):
        if r is None:
            continue
        for ing in r.ingredients:
            if ing.canonical_food_id:
                ids.add(ing.canonical_food_id)
        for sub in r.substitutions:
            ids.add(sub.to_food_id)
    return ids


def _portion_anomalies(meal: Meal, recipe: Recipe, side: Recipe | None, ctx: PlanContext) -> list[str]:
    members = _members(ctx.profile)
    rules = ctx.portion_rules.get("per_person_grams", {})
    by_role: dict[str, float] = {}
    for p in meal.portions:
        role = _role_of(p.food.id, recipe, side)
        if role:
            by_role[role] = by_role.get(role, 0.0) + p.grams
    out: list[str] = []
    for role, grams in by_role.items():
        rng = rules.get(role)
        if not rng:
            continue
        per_person = grams / members
        if per_person > rng["max"] * _SEVERE_OVER:
            out.append(f"severe {role} portion {per_person:.0f} g/person (max {rng['max']})")
        elif per_person < rng["min"] * _SEVERE_UNDER and per_person > 0:
            out.append(f"severe low {role} portion {per_person:.0f} g/person (min {rng['min']})")
    return out
