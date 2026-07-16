"""Recipe catalog domain models.

Mirrors the compiled ``src/data/recipe_index.json`` (built from the read-only
``content/`` markdown by ``scripts/build_recipe_index.py``). Recipes are the
source of truth for recipe-first meal generation: a meal always references a
real recipe here and can be traced back to ``source_file``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from models.food import Nutrients


class RecipeType(str, Enum):
    MAIN_MEAL = "main_meal"
    BREAKFAST = "breakfast"
    SIDE = "side"
    SNACK = "snack"
    DESSERT = "dessert"
    DRINK = "drink"
    SAUCE = "sauce"
    SEASONING = "seasoning"
    BASE = "base"


# Recipe types that may fill a Breakfast / Lunch / Dinner slot as the primary
# dish. sauce/seasoning/drink/dessert/base can never be a meal on their own.
PRIMARY_MEAL_TYPES = {RecipeType.MAIN_MEAL, RecipeType.BREAKFAST}
# Types allowed as a companion "side" within a meal (not as the primary dish).
COMPANION_TYPES = {RecipeType.SIDE, RecipeType.SNACK}


@dataclass(frozen=True)
class RecipeIngredient:
    raw_text: str
    canonical_food_id: str | None
    normalized_id: str | None
    role: str
    grams_per_serving: float | None
    quantity_state: str          # raw | dry | cooked | drained | canned
    nutrition_basis: str | None
    is_core: bool
    is_seasoning: bool
    optional: bool
    match_method: str
    confidence: float
    # Legacy recipe indexes predate this explicit signal and load as food.
    is_nonfood: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "RecipeIngredient":
        return cls(
            raw_text=str(d["raw_text"]),
            canonical_food_id=d.get("canonical_food_id"),
            normalized_id=d.get("normalized_id"),
            role=str(d.get("role", "unknown")),
            grams_per_serving=(float(d["grams_per_serving"]) if d.get("grams_per_serving") is not None else None),
            quantity_state=str(d.get("quantity_state", "raw")),
            nutrition_basis=d.get("nutrition_basis"),
            is_core=bool(d.get("is_core", False)),
            is_seasoning=bool(d.get("is_seasoning", False)),
            optional=bool(d.get("optional", False)),
            match_method=str(d.get("match_method", "unresolved")),
            confidence=float(d.get("confidence", 0.0)),
            is_nonfood=bool(d.get("is_nonfood", False)),
        )


@dataclass(frozen=True)
class Substitution:
    role: str
    from_food_id: str
    to_food_id: str
    rename_search: str
    rename_replace: str

    @classmethod
    def from_dict(cls, d: dict) -> "Substitution":
        rename = d.get("rename", {})
        return cls(
            role=str(d["role"]),
            from_food_id=str(d["from"]),
            to_food_id=str(d["to"]),
            rename_search=str(rename.get("search", "")),
            rename_replace=str(rename.get("replace", "")),
        )


@dataclass(frozen=True)
class Recipe:
    id: str
    canonical_name: str
    source_file: str
    tags: tuple[str, ...]
    recipe_type: RecipeType
    meal_types: tuple[str, ...]
    cuisine: str
    dish_category: str
    cooking_methods: tuple[str, ...]
    servings: int
    prep_time_min: int | None
    cook_time_min: int | None
    image_asset: str | None
    directions: tuple[str, ...]
    ingredients: tuple[RecipeIngredient, ...]
    main_protein: str | None
    main_carbs: tuple[str, ...]
    allow_multiple_main_carbs: bool
    vegetables: tuple[str, ...]
    substitutions: tuple[Substitution, ...]
    batchable: bool
    recommended_batch_servings: int | None
    leftover_storage_days: int | None
    reheat_method: str | None
    nutrition_per_serving: Nutrients
    coverage_by_mass: float
    core_coverage: float
    auto_plannable: bool
    contains_pork: bool
    is_meat_or_fish: bool
    allergen_tags: frozenset[str]
    verified: bool = True

    @property
    def core_ingredient_ids(self) -> frozenset[str]:
        return frozenset(
            i.canonical_food_id for i in self.ingredients
            if i.is_core and i.canonical_food_id and not i.is_nonfood
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        return cls(
            id=str(d["id"]),
            canonical_name=str(d["canonical_name"]),
            source_file=str(d["source_file"]),
            tags=tuple(d.get("tags", [])),
            recipe_type=RecipeType(d["recipe_type"]),
            meal_types=tuple(d.get("meal_types", [])),
            cuisine=str(d.get("cuisine", "international")),
            dish_category=str(d.get("dish_category", "plate")),
            cooking_methods=tuple(d.get("cooking_methods", [])),
            servings=int(d.get("servings", 4)),
            prep_time_min=(int(d["prep_time_min"]) if d.get("prep_time_min") is not None else None),
            cook_time_min=(int(d["cook_time_min"]) if d.get("cook_time_min") is not None else None),
            image_asset=d.get("image_asset"),
            directions=tuple(d.get("directions", [])),
            ingredients=tuple(RecipeIngredient.from_dict(i) for i in d.get("ingredients", [])),
            main_protein=d.get("main_protein"),
            main_carbs=tuple(d.get("main_carbs", [])),
            allow_multiple_main_carbs=bool(d.get("allow_multiple_main_carbs", False)),
            vegetables=tuple(d.get("vegetables", [])),
            substitutions=tuple(Substitution.from_dict(s) for s in d.get("substitutions", [])),
            batchable=bool(d.get("batchable", False)),
            recommended_batch_servings=d.get("recommended_batch_servings"),
            leftover_storage_days=d.get("leftover_storage_days"),
            reheat_method=d.get("reheat_method"),
            nutrition_per_serving=Nutrients.from_dict(d.get("nutrition_per_serving", {})),
            coverage_by_mass=float(d.get("coverage_by_mass", 0.0)),
            core_coverage=float(d.get("core_coverage", 0.0)),
            auto_plannable=bool(d.get("auto_plannable", False)),
            contains_pork=bool(d.get("contains_pork", False)),
            is_meat_or_fish=bool(d.get("is_meat_or_fish", False)),
            allergen_tags=frozenset(str(t).lower() for t in d.get("allergen_tags", [])),
            verified=bool(d.get("verified", True)),
        )
