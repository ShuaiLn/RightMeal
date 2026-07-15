"""Assign per-serving grams, roles, states, and core-ingredient flags.

Core roles (must resolve for an auto-plannable recipe): a meaningful protein,
main carb, major fat, dairy, or major vegetable. Low-quantity seasonings are
never core — an unresolved seasoning does not block planning; an unresolved
core ingredient does (per the approved staples rule).
"""

from __future__ import annotations

from dataclasses import dataclass

from .ingredient_parser import ParsedLine
from .resolver import Resolution

# Registry role -> whether it can be a CORE ingredient (subject to grams gates).
_CORE_ROLES = {"protein", "main_carb", "fat", "dairy", "vegetable"}

# Minimum per-serving grams for a role to count as "core" (major) rather than a
# flavoring/aromatic. Below this, the ingredient is present but not core.
_CORE_MIN_GRAMS = {
    "protein": 25.0,
    "main_carb": 20.0,
    "fat": 8.0,
    "dairy": 20.0,
    "vegetable": 40.0,
}


@dataclass
class ResolvedIngredient:
    raw_text: str
    food_id: str | None
    role: str
    parent_category: str | None
    quantity_state: str          # raw | dry | cooked | drained | canned
    nutrition_basis: str | None  # food id the nutrition is computed on (== food_id today)
    grams_per_serving: float | None
    is_core: bool
    is_seasoning: bool
    match_method: str
    confidence: float
    optional: bool


def grams_per_serving(
    parsed: ParsedLine,
    parent_category: str | None,
    servings: int,
    portion_defaults: dict,
) -> float | None:
    """Total grams of this line / servings. Uses explicit mass first, then
    volume x density, then count x per-unit weight, then absorption defaults."""
    servings = max(servings, 1)
    total: float | None = None

    if parsed.grams_explicit is not None:
        total = parsed.grams_explicit
    elif parsed.ml_explicit is not None:
        dens = _density(parent_category, portion_defaults)
        total = parsed.ml_explicit * dens
    elif parsed.quantity is not None:
        total = _count_to_grams(parsed, parent_category, portion_defaults)
    else:
        # No quantity: "oil for frying", "salt to taste", bare "Snow Peas".
        absorb = portion_defaults.get("absorption_grams_per_serving", {})
        if parent_category in ("oil", "butter") or parsed.optional and "fry" in parsed.raw_text.lower():
            total = absorb.get(parent_category, absorb.get("_default", 5)) * servings
        else:
            return None

    if total is None:
        return None
    return round(total / servings, 2)


def _density(parent_category: str | None, portion_defaults: dict) -> float:
    table = portion_defaults.get("density_g_per_ml", {})
    if parent_category and parent_category in table:
        return float(table[parent_category])
    return float(table.get("_default", 1.0))


def _count_to_grams(parsed: ParsedLine, parent_category: str | None, portion_defaults: dict) -> float | None:
    counts = portion_defaults.get("count_grams", {})
    cat = counts.get(parent_category or "", {})
    default = counts.get("_default", {})
    unit = parsed.unit
    qty = parsed.quantity or 0.0

    if unit is None:
        per = cat.get("_piece", default.get("_piece", 100))
        return qty * per
    # A volume unit slipped through without explicit ml (rare): convert here.
    from .ingredient_parser import _VOLUME_TO_ML  # local import to avoid cycle at top
    if unit in _VOLUME_TO_ML:
        ml = qty * _VOLUME_TO_ML[unit]
        return ml * _density(parent_category, portion_defaults)
    per = cat.get(unit)
    if per is None:
        per = default.get(unit)
    if per is None:
        per = cat.get("_piece", default.get("_piece", 100))
    return qty * per


def classify_ingredient(
    parsed: ParsedLine,
    resolution: Resolution,
    registry: dict,
    servings: int,
    portion_defaults: dict,
) -> ResolvedIngredient:
    food_id = resolution.food_id
    meta = registry.get(food_id, {}) if food_id else {}
    role = meta.get("role", "seasoning") if food_id else "unknown"
    parent = meta.get("parent_category")
    is_seasoning = bool(meta.get("is_seasoning", False))
    default_state = meta.get("default_state", "raw")
    state = parsed.state_hint or default_state

    grams = grams_per_serving(parsed, parent, servings, portion_defaults)

    is_core = False
    if food_id and role in _CORE_ROLES and not is_seasoning:
        threshold = _CORE_MIN_GRAMS.get(role, 20.0)
        if grams is not None and grams >= threshold:
            is_core = True
    # An unresolved ingredient that is clearly a main component by wording is
    # treated as a *potential* core so it blocks planning (caller checks
    # food_id is None + not seasoning-looking).

    return ResolvedIngredient(
        raw_text=parsed.raw_text,
        food_id=food_id,
        role=role,
        parent_category=parent,
        quantity_state=state,
        nutrition_basis=food_id,
        grams_per_serving=grams,
        is_core=is_core,
        is_seasoning=is_seasoning,
        match_method=resolution.match_method,
        confidence=resolution.confidence,
        optional=parsed.optional,
    )
