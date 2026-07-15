"""Review USDA candidates and promote approved ones into the catalog.

Approval is explicit — never a silent auto-accept. Supply a decisions file
mapping each ingredient id to a chosen USDA fdcId (or "reject" / "unresolved"):

    python tools/review_usda_mappings.py --list           # show candidates
    python tools/review_usda_mappings.py --decisions decisions.json

Approved ingredients are written to:
    src/data/usda_food_mappings.json   (the reviewed nutrient record)
    src/data/extended_foods.json       (a full catalog Food, nutrition from USDA,
                                        packages/prices/allergens from the local
                                        default files, group/state from registry)

Only reviewed mappings enter the production catalog.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "src" / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"

_ROLE_TO_GROUP = {
    "protein": "protein", "main_carb": "grains_starchy", "vegetable": "vegetables",
    "fruit": "fruits", "dairy": "dairy_fortified_alt", "fat": "healthy_fats",
    "sweetener": "grains_starchy", "sauce": "vegetables", "liquid": "vegetables",
}
_STATE_TO_PREP = {"raw": "raw", "dry": "raw", "cooked": "cooked",
                  "canned": "canned", "drained": "canned"}
_NUTRIENT_FIELDS = (
    "calories_kcal", "protein_g", "fiber_g", "calcium_mg", "iron_mg",
    "potassium_mg", "vitamin_a_mcg_rae", "vitamin_c_mg", "vitamin_d_mcg",
    "folate_mcg_dfe", "magnesium_mg", "zinc_mg",
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _list_candidates() -> None:
    data = _read(REPORTS_DIR / "usda_candidate_mappings.json")
    for m in data["mappings"]:
        print(f"\n== {m['ingredient_id']} ({m.get('status')}) role={m.get('role')} "
              f"state={m.get('expected_state')}")
        for c in m.get("candidates", []):
            kcal = c["nutrients_per_100g"].get("calories_kcal")
            print(f"   fdcId={c['fdcId']} [{c['dataType']}] {c['description']} | {kcal} kcal/100g")
        if not m.get("candidates"):
            print("   (no candidates — offline or no match; provide FDC_API_KEY and reimport)")


# Roles whose foods must carry calories; 0 kcal means a broken match.
_CALORIE_ROLES = {"protein", "main_carb", "fat", "dairy", "sweetener"}
_STATE_WORDS = ("cooked", "fried", "baked", "roasted", "grilled", "boiled",
                "canned", "dried", "raw")
_STATE_COMPAT = {"raw": {"raw", "fresh"}, "dry": {"dried", "dry", "raw"},
                 "canned": {"canned"}, "cooked": {"cooked"}, "drained": {"drained", "canned"}}


def _validate_candidate(ingredient_id: str, meta: dict, cand: dict) -> tuple[list[str], float, list[str]]:
    """Automatic guardrails (never rely on manual observation alone).

    Returns (hard_reject_reasons, match_confidence, soft_notes). A candidate
    with any hard reason must not enter the catalog; soft issues lower the
    confidence but a human may still approve it.
    """
    hard: list[str] = []
    notes: list[str] = []
    confidence = 1.0
    desc = (cand.get("description") or "").lower()
    nutr = cand.get("nutrients_per_100g", {})
    role = meta.get("role")
    expected_state = meta.get("default_state", "raw")

    # 1. Name consistency: the description must share the FOOD stem — generic
    #    descriptors (fresh/raw/dried/...) don't count, so "Basil, fresh" cannot
    #    satisfy "tomatoes_fresh" via the word "fresh".
    _GENERIC = {"fresh", "raw", "dried", "cooked", "canned", "whole", "ground",
                "all", "purpose", "large", "small", "sweet", "white", "red", "green"}
    ing_tokens = [t for t in re.findall(r"[a-z]+", ingredient_id.replace("_", " "))
                  if len(t) >= 4 and t not in _GENERIC]
    if ing_tokens and not any(tok[:5] in desc for tok in ing_tokens):
        hard.append(f"description {cand.get('description')!r} shares no food word with {ingredient_id!r}")

    # 2. Zero calories for a calorie-bearing food is a broken/Foundation-summary match.
    if role in _CALORIE_ROLES and float(nutr.get("calories_kcal", 0) or 0) <= 0:
        hard.append("0 kcal for a calorie-bearing ingredient")

    # 3. Basic ingredient matched to a branded product or a composite dish.
    if cand.get("dataType") == "Branded":
        notes.append("branded product"); confidence -= 0.1
    if " with " in desc or re.search(r"\b(and|in)\b .*\bsauce\b", desc):
        notes.append("possible composite dish"); confidence -= 0.1

    # 4. Preparation-state mismatch (raw vs cooked/canned/dried).
    cand_state = next((w for w in _STATE_WORDS if re.search(rf"\b{w}\b", desc)), None)
    if cand_state and cand_state not in _STATE_COMPAT.get(expected_state, {expected_state}):
        notes.append(f"state '{cand_state}' vs expected '{expected_state}'"); confidence -= 0.08

    # 5. Key nutrient abnormally missing for the role.
    if role == "protein" and float(nutr.get("protein_g", 0) or 0) <= 0:
        notes.append("protein_g is 0 for a protein"); confidence -= 0.1

    return hard, round(max(confidence, 0.5), 2), notes


def _build_food(ingredient_id: str, nutrients: dict, fdc_id: int | None, registry: dict,
                packages: dict, prices: dict, allergens: dict) -> dict:
    meta = registry[ingredient_id]
    role = meta["role"]
    group = meta.get("food_group") or _ROLE_TO_GROUP.get(role, "vegetables")
    prep = _STATE_TO_PREP.get(meta.get("default_state", "raw"), "raw")

    pkg_list = packages["foods"].get(ingredient_id, packages["default"])
    price_map = prices["prices"].get(ingredient_id, {})
    package_options = []
    for p in pkg_list:
        entry = {"label": p["label"], "grams": p["grams"],
                 "seed_price": price_map.get(p["label"], round(prices["default_per_100g"] * p["grams"] / 100.0, 2))}
        if p.get("ml") is not None:
            entry["ml"] = p["ml"]
        package_options.append(entry)

    diet = allergens["foods"].get(ingredient_id, allergens["default"])
    is_liquid = any("ml" in p for p in pkg_list)
    return {
        "id": ingredient_id,
        "name": ingredient_id.replace("_", " ").capitalize(),
        "food_group": group,
        "prep_state": prep,
        "form": meta.get("default_state", "raw"),
        "fdc_id": int(fdc_id) if fdc_id is not None else None,
        "is_liquid": is_liquid,
        "density_g_per_ml": 1.0 if is_liquid else None,
        "package_options": package_options,
        "max_weekly_grams": 700,
        "allergen_tags": diet["allergen_tags"],
        "lactose": diet["lactose"],
        "vegetarian": diet["vegetarian"],
        "vegan": diet["vegan"],
        "contains_pork": diet["contains_pork"],
        "is_meat_or_fish": diet["is_meat_or_fish"],
        "search_terms": [ingredient_id.replace("_", " ")],
        "nutrients_per_100g": {n: round(float(nutrients.get(n, 0.0)), 3) for n in _NUTRIENT_FIELDS},
    }


def _apply(decisions_path: Path) -> None:
    decisions = _read(decisions_path)
    candidates = _read(REPORTS_DIR / "usda_candidate_mappings.json")
    by_id = {m["ingredient_id"]: m for m in candidates["mappings"]}
    registry = _read(DATA_DIR / "ingredient_registry.json")["roles"]
    packages = _read(DATA_DIR / "ingredient_package_defaults.json")
    prices = _read(DATA_DIR / "ingredient_price_defaults.json")
    allergens = _read(DATA_DIR / "ingredient_allergens.json")

    mappings_out = _read(DATA_DIR / "usda_food_mappings.json") if (DATA_DIR / "usda_food_mappings.json").exists() else {"version": 1, "mappings": {}}
    extended = _read(DATA_DIR / "extended_foods.json")
    foods_by_id = {f["id"]: f for f in extended["foods"]}

    inserted = updated = rejected = 0
    for ing_id, choice in decisions.items():
        if choice in ("reject", "unresolved"):
            continue
        m = by_id.get(ing_id)
        if not m:
            print(f"  ! {ing_id}: not in candidate file, skipping")
            continue
        chosen = next((c for c in m["candidates"] if str(c["fdcId"]) == str(choice)), None)
        if chosen is None:
            print(f"  ! {ing_id}: fdcId {choice} not among candidates, skipping")
            continue

        # Automatic guardrails: refuse an obviously wrong candidate outright.
        hard, confidence, notes = _validate_candidate(ing_id, registry[ing_id], chosen)
        if hard:
            print(f"  ✗ {ing_id}: REJECTED — {'; '.join(hard)}")
            rejected += 1
            continue
        if notes:
            print(f"  ~ {ing_id}: approved with confidence {confidence} ({'; '.join(notes)})")

        nutrients = chosen["nutrients_per_100g"]
        # Review status and match confidence are separate: reviewed=True means a
        # human confirmed use; matchConfidence reflects how exact the match is.
        mappings_out["mappings"][ing_id] = {
            "canonicalIngredientId": ing_id,
            "displayName": ing_id.replace("_", " ").capitalize(),
            "fdcId": chosen["fdcId"],
            "usdaDataType": chosen["dataType"],
            "usdaDescription": chosen["description"],
            "nutrientsPer100g": nutrients,
            "source": "usda_fdc",
            "matchMethod": "human_reviewed",
            "matchConfidence": confidence,
            "matchNotes": notes,
            "reviewed": True,
            "importedAt": date.today().isoformat(),
        }
        # Upsert: insert when new, otherwise update nutrition/fdc_id/state/source
        # in place so extended_foods.json never drifts from the mapping file.
        built = _build_food(ing_id, nutrients, chosen["fdcId"], registry, packages, prices, allergens)
        if ing_id in foods_by_id:
            existing = foods_by_id[ing_id]
            existing["nutrients_per_100g"] = built["nutrients_per_100g"]
            existing["fdc_id"] = built["fdc_id"]
            existing["form"] = built["form"]
            existing["prep_state"] = built["prep_state"]
            updated += 1
        else:
            extended["foods"].append(built)
            foods_by_id[ing_id] = built
            inserted += 1

    (DATA_DIR / "usda_food_mappings.json").write_text(
        json.dumps(mappings_out, ensure_ascii=False, indent=1), encoding="utf-8")
    extended["foods"].sort(key=lambda f: f["id"])
    (DATA_DIR / "extended_foods.json").write_text(
        json.dumps(extended, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"inserted {inserted}, updated {updated}, rejected {rejected} — "
          f"rerun scripts/build_recipe_index.py")


def main() -> None:
    if "--list" in sys.argv:
        _list_candidates()
        return
    if "--decisions" in sys.argv:
        path = Path(sys.argv[sys.argv.index("--decisions") + 1])
        _apply(path)
        return
    print(__doc__)


if __name__ == "__main__":
    main()
