"""Recipe-level batch/leftover suitability.

Batch behavior is a per-recipe property, not a blanket dish_category rule
(review): a category default is only a fallback, overridable per recipe in
recipe_overrides.json.
"""

from __future__ import annotations

from dataclasses import dataclass

# dish_category -> (recommended_batch_servings multiplier basis, storage days, reheat)
_BATCHABLE_DEFAULTS = {
    "soup":  (2, 4, "stovetop"),
    "stew":  (2, 4, "stovetop"),
    "curry": (2, 4, "stovetop"),
    "pasta": (2, 3, "stovetop"),
    "bake":  (2, 3, "oven"),
    "bowl":  (2, 3, "microwave"),
    "roast": (2, 3, "oven"),
}

# Categories that do not keep/reheat well -> never batch by default.
_NON_BATCHABLE = {"salad", "sandwich", "omelette", "breakfast_dish"}


@dataclass
class BatchInfo:
    batchable: bool
    recommended_batch_servings: int | None
    leftover_storage_days: int | None
    reheat_method: str | None


def batch_info(dish_category: str, recipe_type: str, base_servings: int,
               override: dict | None = None) -> BatchInfo:
    if override and "batchable" in override:
        return BatchInfo(
            batchable=bool(override["batchable"]),
            recommended_batch_servings=override.get("recommended_batch_servings"),
            leftover_storage_days=override.get("leftover_storage_days"),
            reheat_method=override.get("reheat_method"),
        )

    if recipe_type not in ("main_meal",) or dish_category in _NON_BATCHABLE:
        return BatchInfo(False, None, None, None)

    default = _BATCHABLE_DEFAULTS.get(dish_category)
    if default is None:
        return BatchInfo(False, None, None, None)
    mult, days, reheat = default
    return BatchInfo(
        batchable=True,
        recommended_batch_servings=max(base_servings * mult, base_servings + 2),
        leftover_storage_days=days,
        reheat_method=reheat,
    )
