"""Per-serving nutrition, coverage, and the auto-plannable gate.

Nutrition is summed only from ingredients that resolve to a food WITH reviewed
nutrition. Nothing is invented: a pending/unresolved food contributes no
nutrients and lowers coverage. A recipe is auto-plannable only when its core
ingredients are fully resolved-with-nutrition and mass coverage is high.
"""

from __future__ import annotations

from dataclasses import dataclass

from .roles import ResolvedIngredient

_NUTRIENT_NAMES = (
    "calories_kcal", "protein_g", "fiber_g", "calcium_mg", "iron_mg",
    "potassium_mg", "vitamin_a_mcg_rae", "vitamin_c_mg", "vitamin_d_mcg",
    "folate_mcg_dfe", "magnesium_mg", "zinc_mg",
)

# An unresolved, non-optional line this big (g/serving) is treated as a core
# gap that blocks auto-planning, even without a known role.
_UNRESOLVED_CORE_GRAMS = 25.0

MIN_MASS_COVERAGE = 0.90


@dataclass
class NutritionResult:
    nutrition_per_serving: dict[str, float]
    coverage_by_mass: float
    core_coverage: float
    auto_plannable: bool
    core_gap_ids: tuple[str, ...]        # resolved core ingredient food ids
    unresolved_core_texts: tuple[str, ...]  # raw texts of unresolved core gaps


def _zero() -> dict[str, float]:
    return {n: 0.0 for n in _NUTRIENT_NAMES}


def compute(
    ingredients: list[ResolvedIngredient],
    nutrition_by_id: dict[str, dict],   # food_id -> nutrients per 100g (has nutrition)
    *,
    meal_types_nonempty: bool,
) -> NutritionResult:
    totals = _zero()
    resolved_mass = 0.0
    total_mass = 0.0

    core_total = 0
    core_resolved = 0
    unresolved_core: list[str] = []
    core_ids: list[str] = []

    for ing in ingredients:
        if ing.optional or ing.is_seasoning or ing.is_nonfood:
            continue
        grams = ing.grams_per_serving or 0.0

        # Core accounting.
        if ing.food_id is None:
            # Unresolved: is it a substantial (likely-core) line?
            if not ing.optional and grams >= _UNRESOLVED_CORE_GRAMS:
                core_total += 1
                unresolved_core.append(ing.raw_text)
            continue

        if ing.is_core:
            core_total += 1
            if ing.food_id in nutrition_by_id:
                core_resolved += 1
                core_ids.append(ing.food_id)
            else:
                unresolved_core.append(ing.raw_text)

        # Mass + nutrient accounting for resolved foods.
        if grams > 0:
            total_mass += grams
            if ing.food_id in nutrition_by_id:
                resolved_mass += grams
                per100 = nutrition_by_id[ing.food_id]
                factor = grams / 100.0
                for n in _NUTRIENT_NAMES:
                    totals[n] += float(per100.get(n, 0.0)) * factor

    coverage_by_mass = (resolved_mass / total_mass) if total_mass > 0 else 0.0
    core_coverage = (core_resolved / core_total) if core_total > 0 else 1.0

    auto_plannable = (
        meal_types_nonempty
        and core_coverage >= 1.0
        and coverage_by_mass >= MIN_MASS_COVERAGE
        and not unresolved_core
        and totals["calories_kcal"] > 0
    )

    return NutritionResult(
        nutrition_per_serving={n: round(totals[n], 2) for n in _NUTRIENT_NAMES},
        coverage_by_mass=round(coverage_by_mass, 4),
        core_coverage=round(core_coverage, 4),
        auto_plannable=auto_plannable,
        core_gap_ids=tuple(core_ids),
        unresolved_core_texts=tuple(unresolved_core),
    )
