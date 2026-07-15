"""Validating loaders for the curated JSON data files.

All paths are resolved relative to this file so loading works identically in
`flet run`, packaged desktop builds, and under pytest.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from models.food import Food, FoodGroup, Nutrients, PackageOption, PrepState

DATA_DIR = Path(__file__).resolve().parent


class DataValidationError(ValueError):
    """A curated data file failed validation."""


def _read_json(filename: str) -> dict:
    path = DATA_DIR / filename
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _build_food(raw: dict) -> Food:
    try:
        packages = tuple(
            PackageOption(
                label=str(p["label"]),
                grams=float(p["grams"]),
                seed_price=float(p["seed_price"]),
                ml=float(p["ml"]) if p.get("ml") is not None else None,
            )
            for p in raw["package_options"]
        )
        return Food(
            id=str(raw["id"]),
            name=str(raw["name"]),
            food_group=FoodGroup(raw["food_group"]),
            prep_state=PrepState(raw["prep_state"]),
            form=str(raw["form"]),
            fdc_id=int(raw["fdc_id"]) if raw.get("fdc_id") is not None else None,
            is_liquid=bool(raw["is_liquid"]),
            density_g_per_ml=(
                float(raw["density_g_per_ml"]) if raw.get("density_g_per_ml") is not None else None
            ),
            package_options=packages,
            max_weekly_grams=float(raw["max_weekly_grams"]),
            allergen_tags=frozenset(str(t).lower() for t in raw["allergen_tags"]),
            lactose=bool(raw["lactose"]),
            vegetarian=bool(raw["vegetarian"]),
            vegan=bool(raw["vegan"]),
            contains_pork=bool(raw["contains_pork"]),
            is_meat_or_fish=bool(raw["is_meat_or_fish"]),
            search_terms=tuple(str(t) for t in raw["search_terms"]),
            nutrients_per_100g=Nutrients.from_dict(raw["nutrients_per_100g"]),
            edible_fraction=float(raw.get("edible_fraction", 1.0)),
            image_url=str(raw["image_url"]) if raw.get("image_url") else None,
            cooked_yield_factor=(
                float(raw["cooked_yield_factor"]) if raw.get("cooked_yield_factor") is not None else None
            ),
            max_plated_grams_per_member_day=(
                float(raw["max_plated_grams_per_member_day"])
                if raw.get("max_plated_grams_per_member_day") is not None
                else None
            ),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise DataValidationError(f"Invalid seed food {raw.get('id', '<missing id>')!r}: {exc}") from exc


@lru_cache(maxsize=1)
def load_seed_foods() -> tuple[Food, ...]:
    data = _read_json("seed_foods.json")
    foods = tuple(_build_food(raw) for raw in data["foods"])
    ids = [f.id for f in foods]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise DataValidationError(f"Duplicate seed food ids: {dupes}")
    missing_groups = set(FoodGroup) - {f.food_group for f in foods}
    if missing_groups:
        raise DataValidationError(
            f"Seed foods must cover all 6 food groups; missing: {sorted(g.value for g in missing_groups)}"
        )
    return foods


@lru_cache(maxsize=1)
def load_extended_foods() -> tuple[Food, ...]:
    """Reviewed catalog foods beyond the 53 seeds (USDA import + review).

    Absent or empty is fine (first run before any USDA review). These carry the
    same schema as seed foods; nutrition is always reviewed, never invented.
    """
    try:
        data = _read_json("extended_foods.json")
    except FileNotFoundError:
        return ()
    return tuple(_build_food(raw) for raw in data.get("foods", []))


@lru_cache(maxsize=1)
def load_catalog() -> tuple[Food, ...]:
    """The full food catalog the app plans with: seed + reviewed extended.

    Ids are unique across both sets; the catalog is strictly additive so old
    plans and pantries keep resolving their food ids.
    """
    seed = load_seed_foods()
    extended = load_extended_foods()
    foods = seed + extended
    ids = [f.id for f in foods]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise DataValidationError(f"Duplicate catalog food ids (seed vs extended): {dupes}")
    return foods


@lru_cache(maxsize=1)
def load_recipe_index():
    """Load and validate the compiled recipe catalog.

    Every ``canonical_food_id`` referenced by a recipe must exist in the food
    catalog, so a meal can never reference a food the app cannot price or track.
    """
    from models.recipe import Recipe  # local import: models depend on data pkg

    data = _read_json("recipe_index.json")
    catalog_ids = {f.id for f in load_catalog()}
    recipes = []
    for raw in data.get("recipes", []):
        recipe = Recipe.from_dict(raw)
        for ing in recipe.ingredients:
            if ing.canonical_food_id and ing.canonical_food_id not in catalog_ids:
                raise DataValidationError(
                    f"Recipe {recipe.id!r} references unknown food id {ing.canonical_food_id!r}"
                )
        recipes.append(recipe)
    ids = [r.id for r in recipes]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise DataValidationError(f"Duplicate recipe ids: {dupes}")
    return tuple(recipes)


@lru_cache(maxsize=1)
def load_portion_rules() -> dict:
    """Per-person portion ranges + slot/daily kcal shares for the validators."""
    return _read_json("portion_rules.json")


@lru_cache(maxsize=1)
def load_bls_price_map() -> dict:
    data = _read_json("bls_price_map.json")
    area = data.get("area_codes", {})
    if "default" not in area or "zip_prefix_to_area" not in area:
        raise DataValidationError("bls_price_map.json must define area_codes.default and zip_prefix_to_area")
    food_ids = {f.id for f in load_seed_foods()}
    for food_id, series in data.get("series", {}).items():
        if food_id not in food_ids:
            raise DataValidationError(f"BLS map references unknown food id {food_id!r}")
        if series is None:
            continue
        for key in ("item_code", "bls_unit", "grams_per_unit"):
            if key not in series:
                raise DataValidationError(f"BLS series for {food_id!r} is missing {key!r}")
    return data


@lru_cache(maxsize=1)
def load_nutrient_targets() -> dict:
    data = _read_json("nutrient_targets.json")
    person_types = data.get("person_types", {})
    for required in ("adult", "child", "senior"):
        if required not in person_types:
            raise DataValidationError(f"nutrient_targets.json missing person type {required!r}")
        targets = person_types[required]
        missing = set(Nutrients.NAMES) - set(targets)
        if missing:
            raise DataValidationError(f"Targets for {required!r} missing nutrients: {sorted(missing)}")
    caps = data.get("group_weekly_caps_g_per_person", {})
    missing_caps = {g.value for g in FoodGroup} - set(caps)
    if missing_caps:
        raise DataValidationError(f"Missing group caps for: {sorted(missing_caps)}")
    return data
