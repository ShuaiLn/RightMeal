"""Dev-time USDA FoodData Central candidate importer.

Generates *candidates only* for the pending ingredients chosen by the unlock
ranking. Nothing here is auto-accepted and nothing is written to the production
catalog: approval happens in tools/review_usda_mappings.py.

    # 1. rank the pending ingredients (writes reports/usda_search_requests.json)
    python scripts/scan_ingredients.py 40
    # 2. fetch candidates (needs FDC_API_KEY in .env; offline it stays pending)
    python tools/import_usda_foods.py

Key handling (never hardcode / commit / package the key):
    FDC_API_KEY is read from the environment / .env only, at dev time.
    Production reads the reviewed local JSON and never calls USDA.

Offline (no key / no network) this writes only pending stubs — it must never
present an empty candidate list as a completed USDA match.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a project dependency
    httpx = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"
SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
PREFERRED_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]

# Canonical USDA-style queries for ingredients whose generic aliases return the
# wrong food (e.g. "fresh tomatoes" surfacing herbs). Tried before the aliases.
QUERY_HINTS = {
    "tomatoes_fresh": ["Tomatoes, red, ripe, raw"],
    "sugar_white": ["Sugars, granulated"],
    "chicken_broth": ["Soup, chicken broth, canned"],
    "bacon": ["Pork, cured, bacon, cooked"],
    "sausage": ["Sausage, Italian, pork, mild, raw"],
    "green_onions": ["Onions, spring or scallions, raw"],
    "tomato_paste": ["Tomato products, canned, paste, without salt added"],
    "tomato_sauce": ["Tomato products, canned, sauce"],
    "heavy_cream": ["Cream, fluid, heavy whipping"],
    "butter": ["Butter, salted"],
    "flour_all_purpose": ["Flour, wheat, all-purpose, enriched, bleached"],
    "lemon": ["Lemons, raw, without peel"],
    "lime": ["Limes, raw"],
    "mushrooms": ["Mushrooms, white, raw"],
}

# USDA nutrient number -> our 12 nutrient fields (per 100 g).
_NUTRIENT_NUMBERS = {
    "208": "calories_kcal", "203": "protein_g", "291": "fiber_g",
    "301": "calcium_mg", "303": "iron_mg", "306": "potassium_mg",
    "320": "vitamin_a_mcg_rae", "401": "vitamin_c_mg", "328": "vitamin_d_mcg",
    "435": "folate_mcg_dfe", "304": "magnesium_mg", "309": "zinc_mg",
}
_NUTRIENT_FIELDS = tuple(dict.fromkeys(_NUTRIENT_NUMBERS.values()))
# Foundation foods often omit 208 in the search summary and report energy under
# Atwater numbers instead; use them as a kcal fallback so kcal is never 0 by
# accident. Ordered by preference.
_ENERGY_FALLBACK = ("957", "958", "2047", "2048")


def _extract_nutrients(food: dict) -> dict[str, float]:
    out = {n: 0.0 for n in _NUTRIENT_FIELDS}
    by_number: dict[str, float] = {}
    for fn in food.get("foodNutrients", []):
        number = str(fn.get("nutrientNumber") or fn.get("number") or "")
        try:
            value = float(fn.get("value", fn.get("amount", 0)) or 0)
        except (TypeError, ValueError):
            continue
        by_number[number] = value
        if number in _NUTRIENT_NUMBERS:
            out[_NUTRIENT_NUMBERS[number]] = value
    if not out["calories_kcal"]:
        for num in _ENERGY_FALLBACK:
            if by_number.get(num):
                out["calories_kcal"] = by_number[num]
                break
    return out


def _search_once(client, api_key: str, query: str) -> list[dict]:
    params = {"query": query, "dataType": PREFERRED_TYPES, "pageSize": 8, "api_key": api_key}
    # FDC intermittently returns 400/429/5xx under load; back off and retry.
    resp = None
    for attempt in range(4):
        resp = client.get(SEARCH_URL, params=params, timeout=30.0)
        if resp.status_code in (400, 429, 500, 502, 503, 504):
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    assert resp is not None
    resp.raise_for_status()
    return resp.json().get("foods", [])


def _fetch_candidates(client, api_key: str, terms: list[str]) -> list[dict]:
    # Try each search term (best/most-specific first) until one returns hits.
    foods: list[dict] = []
    for query in terms or [""]:
        try:
            foods = _search_once(client, api_key, query)
        except Exception:  # noqa: BLE001 - try the next term
            foods = []
        if foods:
            break
    # Preserve the data-type preference order; keep several for human review.
    foods.sort(key=lambda f: PREFERRED_TYPES.index(f["dataType"])
               if f.get("dataType") in PREFERRED_TYPES else 99)
    return [
        {
            "fdcId": f.get("fdcId"),
            "description": f.get("description"),
            "dataType": f.get("dataType"),
            "nutrients_per_100g": _extract_nutrients(f),
        }
        for f in foods[:6]
    ]


def main() -> None:
    requests_path = REPORTS_DIR / "usda_search_requests.json"
    if not requests_path.exists():
        raise SystemExit("Run scripts/scan_ingredients.py first to produce usda_search_requests.json")
    requests = json.loads(requests_path.read_text(encoding="utf-8"))["requests"]

    api_key = os.environ.get("FDC_API_KEY", "").strip()
    out_path = REPORTS_DIR / "usda_candidate_mappings.json"

    if not api_key or httpx is None:
        reason = "no FDC_API_KEY" if not api_key else "httpx unavailable"
        payload = {
            "status": "pending",
            "reason": f"Offline: {reason}. No USDA candidates fetched; these are "
                      f"NOT completed matches. Set FDC_API_KEY in .env and rerun.",
            "mappings": [
                {**r, "candidates": [], "status": "pending"} for r in requests
            ],
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[offline] wrote {out_path.name} with {len(requests)} pending ingredients "
              f"({reason}). Provide FDC_API_KEY to fetch real candidates.")
        return

    # Merge across runs: keep candidates already fetched, only (re)try the ones
    # still empty. Running the importer again converges past transient burst
    # limits without re-hitting ingredients that already succeeded.
    prior: dict[str, dict] = {}
    if out_path.exists():
        for m in json.loads(out_path.read_text(encoding="utf-8")).get("mappings", []):
            prior[m["ingredient_id"]] = m

    mappings = []
    with httpx.Client() as client:
        for r in requests:
            existing = prior.get(r["ingredient_id"])
            if existing and existing.get("candidates"):
                mappings.append(existing)
                print(f"  {r['ingredient_id']:<20} cached ({len(existing['candidates'])} candidates)")
                continue
            try:
                terms = QUERY_HINTS.get(r["ingredient_id"], []) + r["search_terms"]
                candidates = _fetch_candidates(client, api_key, terms)
                status = "needs_review" if candidates else "no_match"
            except Exception as exc:  # noqa: BLE001 - report, never crash the batch
                code = getattr(getattr(exc, "response", None), "status_code", "")
                candidates, status = [], f"error: {type(exc).__name__} {code}".strip()
            mappings.append({**r, "candidates": candidates, "status": status})
            print(f"  {r['ingredient_id']:<20} {status} ({len(candidates)} candidates)")
            time.sleep(1.2)  # gentle pacing to stay under the FDC burst limit

    out_path.write_text(json.dumps(
        {"status": "needs_review", "mappings": mappings}, ensure_ascii=False, indent=1
    ), encoding="utf-8")
    got = sum(1 for m in mappings if m.get("candidates"))
    print(f"wrote {out_path.name}: {got}/{len(mappings)} ingredients have candidates. "
          f"Rerun to fill any gaps; then review. Nothing entered the catalog yet.")


if __name__ == "__main__":
    main()
