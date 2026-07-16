"""Rank pending ingredients by recipe-unlock and emit USDA search requests.

Reads the compiled recipe_index.json, finds every main-meal / breakfast recipe
that is blocked only by pending ingredients (resolvable but lacking nutrition),
and runs the iterative unlock ranking to choose the top-N to import from USDA.

    python scripts/scan_ingredients.py [N]

Outputs:
    reports/top_40_ingredient_priority_report.json
    reports/usda_search_requests.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recipe_indexer.unlock import RecipeGap, rank_ingredients
from recipe_indexer.nutrition import _UNRESOLVED_CORE_GRAMS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "src" / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"


def _read(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _nutrition_ids() -> set[str]:
    ids = set()
    for fname in ("seed_foods.json", "extended_foods.json"):
        for food in _read(DATA_DIR / fname)["foods"]:
            ids.add(food["id"])
    return ids


def collect_gaps() -> list[RecipeGap]:
    index = _read(DATA_DIR / "recipe_index.json")
    have_nutrition = _nutrition_ids()
    gaps: list[RecipeGap] = []
    for r in index["recipes"]:
        if not r["meal_types"] or r["auto_plannable"]:
            continue
        # Any resolved-but-no-nutrition non-seasoning ingredient blocks the
        # recipe: core ones fail core_coverage, the rest fail mass coverage.
        # Giving all of them reviewed nutrition is what unlocks the recipe.
        pending: set[str] = set()
        unresolved_core = False
        for ing in r["ingredients"]:
            if ing["optional"] or ing["is_seasoning"] or ing.get("is_nonfood", False):
                continue
            fid = ing["canonical_food_id"]
            grams = ing["grams_per_serving"] or 0.0
            if fid is None:
                if grams >= _UNRESOLVED_CORE_GRAMS:
                    unresolved_core = True
                continue
            if fid not in have_nutrition:
                pending.add(fid)
        gaps.append(RecipeGap(
            recipe_id=r["id"],
            slot_types=tuple(r["meal_types"]),
            pending_core_ids=pending,
            has_unresolved_core=unresolved_core,
        ))
    return gaps


def _search_terms(ing_id: str, registry: dict, aliases: dict) -> list[str]:
    # Reverse the alias map to recover human search phrases for the id.
    phrases = [a for a, fid in aliases.items() if fid == ing_id]
    phrases.append(ing_id.replace("_", " "))
    return sorted(set(phrases))


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    registry = _read(DATA_DIR / "ingredient_registry.json")["roles"]
    aliases = _read(DATA_DIR / "ingredient_aliases.json")["aliases"]
    gaps = collect_gaps()
    ranked = rank_ingredients(gaps, registry, limit=limit)

    # How many recipes each cumulative prefix would unlock.
    live = {g.recipe_id: set(g.pending_core_ids) for g in gaps
            if g.pending_core_ids and not g.has_unresolved_core}
    chosen: set[str] = set()
    cumulative = []
    for r in ranked:
        chosen.add(r.ingredient_id)
        unlocked = sum(1 for ids in live.values() if ids <= chosen)
        cumulative.append(unlocked)

    REPORTS_DIR.mkdir(exist_ok=True)
    priority = {
        "total_blocked_recipes": len(live),
        "blocked_by_unresolved_alias": sum(
            1 for g in gaps if g.has_unresolved_core and not (g.pending_core_ids <= set())
        ),
        "ranking": [
            {
                "rank": i + 1,
                "ingredient_id": r.ingredient_id,
                "role": r.role,
                "default_state": registry.get(r.ingredient_id, {}).get("default_state"),
                "score": r.score,
                "immediate_unlocks": r.immediate_unlocks,
                "recipes_touched": r.recipes_touched,
                "cumulative_recipes_unlocked": cumulative[i],
            }
            for i, r in enumerate(ranked)
        ],
    }
    (REPORTS_DIR / "top_40_ingredient_priority_report.json").write_text(
        json.dumps(priority, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    search_requests = {
        "notes": "Offline stub. Feed these to tools/import_usda_foods.py; real "
                 "candidates require FDC_API_KEY. Never treat an empty candidate "
                 "list as a completed match.",
        "requests": [
            {
                "ingredient_id": r.ingredient_id,
                "role": r.role,
                "expected_state": registry.get(r.ingredient_id, {}).get("default_state"),
                "search_terms": _search_terms(r.ingredient_id, registry, aliases),
                "preferred_data_types": ["Foundation", "SR Legacy", "Survey (FNDDS)"],
                "status": "pending",
                "candidates": [],
            }
            for r in ranked
        ],
    }
    (REPORTS_DIR / "usda_search_requests.json").write_text(
        json.dumps(search_requests, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"blocked recipes: {len(live)} | ranked {len(ranked)} | "
          f"cumulative unlock at N={len(ranked)}: {cumulative[-1] if cumulative else 0}")
    for row in priority["ranking"][:25]:
        print(f"  #{row['rank']:>2} {row['ingredient_id']:<20} {row['role']:<10} "
              f"score={row['score']:<8} immediate={row['immediate_unlocks']:<3} "
              f"cum_unlocked={row['cumulative_recipes_unlocked']}")


if __name__ == "__main__":
    main()
