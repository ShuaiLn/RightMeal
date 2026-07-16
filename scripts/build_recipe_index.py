"""Compile content/*.md into src/data/recipe_index.json + coverage reports.

Dev-time only. Reads the read-only recipe markdown and the curated config
files, resolves + classifies + costs every recipe, and writes a single cached
JSON the app loads at runtime. The markdown is never modified.

    python scripts/build_recipe_index.py

Outputs:
    src/data/recipe_index.json
    reports/recipe_coverage_report.json
    reports/unresolved_ingredients_report.json
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recipe_indexer.md_parser import parse_recipe_md, iter_recipe_paths
from recipe_indexer.ingredient_parser import parse_ingredient_line
from recipe_indexer.resolver import IngredientResolver, normalize, Resolution
from recipe_indexer.roles import classify_ingredient
from recipe_indexer import classifier as clf
from recipe_indexer.batch import batch_info
from recipe_indexer import nutrition as nut

INDEX_VERSION = 2
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "src" / "data"
CONTENT_DIR = PROJECT_ROOT / "content"
REPORTS_DIR = PROJECT_ROOT / "reports"


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _build_resolver_inputs():
    seed = _read_json(DATA_DIR / "seed_foods.json")["foods"]
    extended = _read_json(DATA_DIR / "extended_foods.json")["foods"]
    registry = _read_json(DATA_DIR / "ingredient_registry.json")["roles"]
    aliases = _read_json(DATA_DIR / "ingredient_aliases.json")["aliases"]

    catalog_terms: dict[str, str] = {}
    nutrition_by_id: dict[str, dict] = {}
    for food in seed + extended:
        fid = food["id"]
        catalog_terms[normalize(food["name"])] = fid
        for term in food.get("search_terms", []):
            catalog_terms.setdefault(normalize(term), fid)
        nutrition_by_id[fid] = food["nutrients_per_100g"]

    known_ids = set(nutrition_by_id) | set(registry)
    resolver = IngredientResolver(catalog_terms, aliases, {}, known_ids)
    return resolver, registry, nutrition_by_id


def _has_role(resolved, role: str) -> bool:
    return any(r.role == role and (r.is_core or r.food_id) for r in resolved)


def build() -> dict:
    resolver, registry, nutrition_by_id = _build_resolver_inputs()
    portion_defaults = _read_json(DATA_DIR / "ingredient_portion_defaults.json")
    recipe_overrides = _read_json(DATA_DIR / "recipe_overrides.json")["overrides"]
    ing_overrides = _read_json(DATA_DIR / "ingredient_overrides.json")["overrides"]

    entries: list[dict] = []
    unresolved_counter: collections.Counter = collections.Counter()
    coverage_rows: list[dict] = []

    for path in iter_recipe_paths(CONTENT_DIR):
        raw = parse_recipe_md(path, PROJECT_ROOT)
        servings = raw.servings or 4
        line_overrides = ing_overrides.get(raw.slug, {})

        resolved = []
        for line in raw.raw_ingredients:
            parsed = parse_ingredient_line(line)
            ov = line_overrides.get(line)
            if ov and "food_id" in ov:
                res = Resolution(ov["food_id"], "override", 1.0)
            else:
                res = resolver.resolve(parsed.name)
            ri = classify_ingredient(parsed, res, registry, servings, portion_defaults)
            if ov and not ri.is_nonfood:
                if "role" in ov:
                    ri.role = ov["role"]
                if "is_core" in ov:
                    ri.is_core = bool(ov["is_core"])
                if "grams_per_serving" in ov:
                    ri.grams_per_serving = ov["grams_per_serving"]
            resolved.append(ri)
            if ri.food_id is None and not ri.optional and not ri.is_nonfood and ri.grams_per_serving \
                    and ri.grams_per_serving >= nut._UNRESOLVED_CORE_GRAMS:
                unresolved_counter[parsed.name] += 1

        # The index only records catalog food ids (those with reviewed
        # nutrition). Seasonings and still-pending items resolve for
        # classification but are stored as name-only (canonical_food_id=null):
        # they never enter portions, nutrition, or inventory.
        catalog_ids = set(nutrition_by_id)

        has_protein = _has_role(resolved, "protein")
        has_carb = _has_role(resolved, "main_carb")
        has_veg = _has_role(resolved, "vegetable")

        rov = recipe_overrides.get(raw.slug)
        cls = clf.classify(
            raw.title, raw.tags, raw.directions,
            has_protein=has_protein, has_main_carb=has_carb, has_vegetable=has_veg,
            override=rov,
        )
        nutrition = nut.compute(
            resolved, nutrition_by_id, meal_types_nonempty=bool(cls.meal_types)
        )
        binfo = batch_info(cls.dish_category, cls.recipe_type, servings, override=rov)

        main_carbs = [r.food_id for r in resolved if r.role == "main_carb" and r.is_core and r.food_id in catalog_ids]
        main_proteins = [r.food_id for r in resolved if r.role == "protein" and r.is_core and r.food_id in catalog_ids]
        vegetables = [r.food_id for r in resolved if r.role == "vegetable" and r.is_core and r.food_id in catalog_ids]
        allow_multi = bool(rov.get("allow_multiple_main_carbs", False)) if rov else False

        image_asset = f"recipe_images/{raw.image_slug}.webp" if raw.image_slug else None

        # Dietary flags derived from resolved foods (union). Seasonings ignored.
        allergen_tags: set[str] = set()
        contains_pork = any(r.parent_category == "pork" for r in resolved)
        is_meat_or_fish = any(
            r.role == "protein" and r.parent_category in
            {"beef", "chicken", "pork", "turkey", "fish", "shellfish"}
            for r in resolved
        )

        entries.append({
            "id": raw.slug,
            "canonical_name": raw.title,
            "source_file": raw.source_file,
            "tags": list(raw.tags),
            "recipe_type": cls.recipe_type,
            "meal_types": list(cls.meal_types),
            "cuisine": cls.cuisine,
            "dish_category": cls.dish_category,
            "cooking_methods": list(cls.cooking_methods),
            "servings": servings,
            "prep_time_min": raw.prep_time_min,
            "cook_time_min": raw.cook_time_min,
            "image_asset": image_asset,
            "directions": list(raw.directions),
            "ingredients": [
                {
                    "raw_text": r.raw_text,
                    "canonical_food_id": r.food_id if r.food_id in catalog_ids else None,
                    # The resolved registry id even when not a catalog food (e.g.
                    # seasonings): used for clean staple names, never for nutrition.
                    "normalized_id": r.food_id,
                    "role": r.role,
                    "grams_per_serving": r.grams_per_serving,
                    "quantity_state": r.quantity_state,
                    "nutrition_basis": r.nutrition_basis,
                    "is_core": r.is_core,
                    "is_seasoning": r.is_seasoning,
                    "is_nonfood": r.is_nonfood,
                    "optional": r.optional,
                    "match_method": r.match_method,
                    "confidence": round(r.confidence, 3),
                }
                for r in resolved
            ],
            "main_protein": main_proteins[0] if main_proteins else None,
            "main_carbs": main_carbs,
            "allow_multiple_main_carbs": allow_multi,
            "vegetables": vegetables,
            "substitutions": [],
            "batchable": binfo.batchable,
            "recommended_batch_servings": binfo.recommended_batch_servings,
            "leftover_storage_days": binfo.leftover_storage_days,
            "reheat_method": binfo.reheat_method,
            "nutrition_per_serving": nutrition.nutrition_per_serving,
            "coverage_by_mass": nutrition.coverage_by_mass,
            "core_coverage": nutrition.core_coverage,
            "auto_plannable": nutrition.auto_plannable,
            "contains_pork": contains_pork,
            "is_meat_or_fish": is_meat_or_fish,
            "allergen_tags": sorted(allergen_tags),
            "verified": True,
        })
        coverage_rows.append({
            "id": raw.slug, "recipe_type": cls.recipe_type,
            "meal_types": list(cls.meal_types),
            "auto_plannable": nutrition.auto_plannable,
            "core_coverage": nutrition.core_coverage,
            "coverage_by_mass": nutrition.coverage_by_mass,
            "unresolved_core": list(nutrition.unresolved_core_texts),
            "nonfood": [r.raw_text for r in resolved if r.is_nonfood],
        })

    ids = [e["id"] for e in entries]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"Duplicate recipe slugs: {dupes}")

    index = {"index_version": INDEX_VERSION, "built_from": "content/", "recipes": entries}
    return {"index": index, "coverage": coverage_rows, "unresolved": unresolved_counter}


def _summary(coverage: list[dict]) -> dict:
    by_type: collections.Counter = collections.Counter()
    plannable_by_slot: collections.Counter = collections.Counter()
    for row in coverage:
        by_type[row["recipe_type"]] += 1
        if row["auto_plannable"]:
            for slot in row["meal_types"]:
                plannable_by_slot[slot] += 1
    return {
        "total_recipes": len(coverage),
        "auto_plannable": sum(1 for r in coverage if r["auto_plannable"]),
        "nonfood_lines": sum(len(r.get("nonfood", ())) for r in coverage),
        "by_recipe_type": dict(by_type),
        "auto_plannable_by_slot": dict(plannable_by_slot),
    }


def main() -> None:
    result = build()
    REPORTS_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "recipe_index.json").write_text(
        json.dumps(result["index"], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    summary = _summary(result["coverage"])
    (REPORTS_DIR / "recipe_coverage_report.json").write_text(
        json.dumps({"summary": summary, "recipes": result["coverage"]},
                   ensure_ascii=False, indent=1), encoding="utf-8"
    )
    top_unresolved = result["unresolved"].most_common(120)
    (REPORTS_DIR / "unresolved_ingredients_report.json").write_text(
        json.dumps({"count": len(result["unresolved"]),
                    "top_unresolved_core": [{"name": n, "recipes": c} for n, c in top_unresolved]},
                   ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("recipe_index.json written:", summary)


if __name__ == "__main__":
    main()
