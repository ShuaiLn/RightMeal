# RightMeal

RightMeal is a grocery **planning and nutrition affordability** tool built with
Python and [Flet](https://flet.dev). Give it your household, a budget, a U.S.
ZIP code, and a planning horizon — it prices a curated list of everyday foods,
optimizes a basket for nutrition adequacy **within your budget**, and explains
the result in plain English.

It is a planning aid, not a checkout app: there are no purchase links, no
carts, and no accounts.

## Important disclaimers

- **Thrifty-Food-Plan-inspired, not the USDA TFP.** RightMeal borrows the
  *structure* of the USDA Thrifty Food Plan (food-group coverage, weekly
  quantity caps, diversity, gap reporting) but does not claim to reproduce it.
- **The basket total is an estimated planning total.** Item prices can come
  from different sources (Kroger, Instacart, BLS averages, seed estimates), so
  the total is never a single store's real checkout total.
- **The optimizer scoring is an MVP model, not an official nutrition scoring
  system.** Its weights (see `src/optimizer/config.py`) are practical planning
  heuristics and are open to debate.
- **No medical claims.** Nutrition targets are simplified, gender-averaged
  DRI-based values for planning only. RightMeal gives no medical, diagnostic,
  or treatment advice and promises no health outcomes.
- **Privacy.** Your profile (household members, restrictions, allergies, ZIP,
  optional API keys) is stored **locally only** at
  `%APPDATA%\RightMeal\profile.json` (or `~/.rightmeal/`). Delete that file —
  or use the *Delete saved data* button on the Profile page — to remove it.
  Keys entered in-app are saved in that local file in plain text; prefer the
  `.env` file if that concerns you.

## Run the app

```bash
uv sync --all-groups          # once: create the venv and install dependencies
uv run flet run               # desktop app
uv run flet run --web         # or in the browser
```

First launch shows a one-time onboarding (household members, dietary
restrictions, allergies, default city/ZIP). After that you land on the
planning page: enter a budget (daily or weekly), optionally override the ZIP,
choose how many days to plan for, and press **Plan my groceries**.

## API keys (all optional)

The app is fully functional with **zero** keys — prices then come from BLS
regional averages (public API, no key needed) and curated seed estimates.
Keys unlock better data:

| Key | Unlocks |
|---|---|
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | Real Kroger/Ralphs store prices near your ZIP |
| `INSTACART_API_KEY` | Instacart product matching + numeric prices |
| `FDC_API_KEY` | Optional USDA FoodData Central enrichment (curated data always wins) |
| `OPENAI_API_KEY` | AI-written explanations (falls back to local templates) |
| `BLS_API_KEY` | Higher BLS rate limits (works without it) |

Copy `.env.example` to `.env` and fill in what you have, or paste keys into
the password-masked fields on the Profile page (in-app values take precedence
over environment variables).

## How pricing works

For each food, providers are tried in strict priority order and the first
usable quote wins:

1. **Kroger real product price** — live store price, matched by name.
2. **Instacart numeric product price** — only if the product has BOTH a
   numeric price and a parseable size (no ranges, no "each").
3. **BLS regional average estimate** — only for foods explicitly mapped in
   `src/data/bls_price_map.json`; unmapped foods always skip BLS.
4. **Seed estimate** — curated fallback that always succeeds.

Quotes matched with confidence below **0.65** are rejected and fall through.
Every basket row shows its price source and match confidence, and responses
are cached per session (provider + location + food) to avoid repeated calls.

## How the optimizer works

A deterministic pure-Python heuristic (no solver dependencies): greedy growth
by marginal nutrition-per-dollar, a food-group coverage repair pass, then
bounded swap/remove/top-up local search. The budget is a hard constraint;
allergies and diet rules (vegetarian, no pork, lactose-free) are hard
exclusions that are never traded off. Food-group coverage (≥5 of 6), variety
(≥7 distinct foods for a family), and the 35% single-item dominance cap are
soft constraints: enforced when feasible, honestly reported when the budget is
too low. Results distinguish **budget-feasible** ("something affordable
exists") from **nutrition-feasible** ("targets met") — a tight budget yields
"within budget, nutrition partially met" plus an explicit gap list, never a
silent failure.

## Tests

```bash
uv run pytest -q
```

147 tests cover the price fallback chain, confidence thresholds, BLS mapping
rules, unit normalization (lb/oz/dozen/gallon/per-100 g/per-100 ml), allergy
hard exclusions, the $50-week Los Angeles family acceptance case, low-budget
honesty, determinism, and every explanation fallback mode. No test touches
the network — all HTTP is mocked.

## Project structure

```
src/
  main.py          entry point (loads .env, starts Flet)
  theme.py         white / light-green / light-yellow palette
  models/          frozen dataclasses + enums (foods, quotes, baskets, profile)
  data/            curated JSON (35 seed foods, BLS map, nutrient targets) + validating loaders
  services/        price providers & engine, nutrition, units, matching, cache,
                   profile store, explanation services
  optimizer/       config, hard-exclusion filters, scoring, greedy + local search
  ui/              onboarding / planning / profile views, shared components
tests/             pytest suite (mocked HTTP, no network)
```

## Building installers

See the [Flet packaging guides](https://flet.dev/docs/publish/) —
`flet build windows|macos|linux|web|apk|ipa`. The curated JSON ships inside
`src/data/` and is read relative to the module, so packaged builds work
without extra configuration.

## Data sources

- USDA Thrifty Food Plan (structure inspiration): https://www.fns.usda.gov/cnpp/thrifty-food-plan-2021
- Dietary Guidelines for Americans: https://www.dietaryguidelines.gov/
- USDA FoodData Central: https://fdc.nal.usda.gov/api-guide
- NIH Dietary Reference Intakes: https://ods.od.nih.gov/HealthInformation/nutrientrecommendations.aspx
- BLS Average Prices: https://www.bls.gov/cpi/factsheets/average-prices.htm
- Kroger API: https://developer.kroger.com/
- Instacart Developer Platform: https://docs.instacart.com/developer_platform_api
- FAO/WHO Sustainable Healthy Diets: https://www.who.int/publications/i/item/9789241516648
