# RightMeal

RightMeal is a grocery **planning and nutrition affordability** tool built with
Python and [Flet](https://flet.dev). Give it your household, a budget, a U.S.
ZIP code, and a date range — it prices a curated catalog of 53 everyday foods,
optimizes a shopping basket for nutrition adequacy **within your budget**,
turns that basket into named breakfast / lunch / dinner meals on a calendar,
and then closes the loop: it tracks what you buy (including by photographing
products or receipts), what you cook, and what's left over, so the next plan
spends money only on what you actually need.

It is a planning aid, not a checkout app: there are no purchase links, no
carts, and no accounts.

## Important disclaimers

- **Thrifty-Food-Plan-inspired, not the USDA TFP.** RightMeal borrows the
  *structure* of the USDA Thrifty Food Plan (food-group coverage, weekly
  quantity caps, diversity, gap reporting) but does not claim to reproduce it.
- **The basket total is an estimated planning total.** Item prices can come
  from different sources (Kroger, Instacart, BLS averages, seed estimates), so
  the total is never a single store's real checkout total.
- **The optimizer and meal-scheduler scoring are MVP models**, not official
  nutrition scoring systems. Their weights are practical planning heuristics
  and are open to debate.
- **No medical claims.** Nutrition targets are simplified, gender-averaged
  DRI-based values for planning only. RightMeal gives no medical, diagnostic,
  or treatment advice and promises no health outcomes.
- **AI cooking steps are for reference only.** Generated steps are plain
  cooking suggestions, not tested or verified recipes — use your own judgment
  on temperatures, doneness, and food safety.
- **AI features are optional and explicit.** With an OpenAI key configured,
  four features send data to OpenAI: plan explanations (basket summary),
  cooking steps (a meal's name, ingredients, and gram amounts), leftover
  notes (your note plus the meal's ingredient list), and photo purchase
  entry (the photo you pick). Every send happens only on a direct user
  action, and a corner notification confirms it. Leave the key unset and the
  app stays fully local: explanations fall back to built-in templates,
  leftovers use a manual percentage slider, and purchases are entered by hand.

## The five pages

- **Start** — budget (daily or weekly), city/ZIP, and a date-range picker;
  one button builds and saves the plan.
- **Plan** — the shopping basket (split into *Use from pantry* and *Need to
  buy*), a budget bar, per-day meal cards with photos, cooking steps, and
  eaten/leftover tracking, a per-day nutrition panel, and honest reporting of
  foods that didn't make the basket.
- **Pantry** — what's left of every food at home; edit grams, remove foods,
  add catalog foods by hand, or add purchases by photo.
- **Calendar** — the saved plan laid out on real month dates, read-only, with
  eaten/leftover status reflected and deep links back to the right plan day.
- **Profile** — household members, dietary restrictions, allergies, default
  location, optional API keys, and a *Delete saved data* button.

First launch shows a one-time onboarding (household, restrictions, allergies,
default city/ZIP) before landing on the Start page.

## Run the app

```bash
uv sync --all-groups          # once: create the venv and install dependencies
uv run flet run               # desktop app
uv run flet run --web         # or in the browser
```

Requires Python 3.10+. Dependencies are minimal: Flet for the UI, httpx for
HTTP, python-dotenv for configuration — no solver or ML libraries.

## API keys (all optional)

The app is fully functional with **zero** keys — prices then come from BLS
regional averages (public API, no key needed) and curated seed estimates.
Keys unlock better data:

| Key | Unlocks |
|---|---|
| `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` | Real Kroger/Ralphs store prices near your ZIP |
| `INSTACART_API_KEY` | Instacart product matching + numeric prices |
| `FDC_API_KEY` | Optional USDA FoodData Central enrichment (curated data always wins) |
| `OPENAI_API_KEY` | AI explanations, cooking steps, leftover-note estimates, photo/receipt purchase entry |
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
3. **BLS regional average estimate** — only for foods explicitly listed in a
   curated BLS mapping; unmapped foods always skip BLS.
4. **Seed estimate** — curated fallback that always succeeds.

Quotes matched with confidence below **0.65** are rejected and fall through.
Package sizes are normalized across units (lb, oz, dozen, gallon, per-100 g,
per-100 ml) so every quote becomes a comparable price per gram. Every basket
row shows its price source and match confidence, and responses are cached per
session (provider + location + food) to avoid repeated calls.

## How the optimizer works

RightMeal does not depend on a professional solver — the optimizer is a
deterministic pure-Python heuristic:

- **Pure Python, no solver.** No LP/MIP dependency, no randomness, no
  wall-clock cutoffs; the same inputs always produce the same basket.
- **Greedy growth.** Packages are added one at a time by marginal
  nutrition-per-dollar.
- **Coverage repair.** A repair pass swaps in missing food groups
  (≥5 of 6 covered whenever the budget allows).
- **Bounded local search.** A final pass tries swaps, removals, and top-ups,
  keeping only changes that improve the overall score.
- **The budget is a hard cap.** The basket total never exceeds it.
- **Allergies and diet rules are hard exclusions.** Allergy tags and diet
  rules (vegetarian, no pork, lactose-free) are filtered out before
  optimization and are never traded off against price or nutrition.
- **Diversity and coverage are soft constraints.** Food-group coverage,
  variety (≥7 distinct foods for a family), and the 35% single-item dominance
  cap are enforced when feasible and honestly reported when the budget is too
  low.

Results distinguish two kinds of feasibility:

- **Budget-feasible** — something affordable exists within the budget.
- **Nutrition-feasible** — the nutrition targets are actually met.

When the budget is too low, RightMeal never pretends the plan is complete: it
reports "within budget, nutrition partially met" plus an explicit list of the
nutrients that fall short — never a silent failure.

## From basket to meals

A second deterministic scheduler turns the optimized basket into named meals
for every day of the plan:

- **Templates, not free association.** Meals are built from hand-written
  dish templates grounded in the food catalog (stir-fries, pasta, oatmeal,
  salads, tacos…). Each template defines component roles with eligible foods,
  a share of the meal's calories, and per-member portion bounds so plates stay
  realistic. Awkward combinations are ruled out structurally — cooking oil
  appears only as the fat of a cooked dish, canned tomatoes never land in a
  salad, raisins only at breakfast.
- **Chronological greedy fill + bounded local search.** Each slot picks the
  best-scoring template given remaining supply; scoring rewards hitting the
  slot's calorie target and draining oversupplied foods, and penalizes
  repeating yesterday's same-slot dish or overusing one template. A bounded
  improvement pass then swaps or tops up meals. No randomness, no wall-clock
  cutoffs — the same inputs always produce the same plan.
- **Batch cooking.** Dinners can be cooked in a double batch, producing a
  named leftover lunch the next day.
- **Sides.** Meals landing below ~88% of their slot's calorie target are
  topped up with small sides (fruit, yogurt, milk) when supply allows.
- **Conservation.** Portions are tracked on the purchased (dry/raw) basis and
  clamped to remaining supply, so scheduled meals plus pantry carryover equal
  the basket exactly. Cooked weights (rice, oats, pasta, beans) are shown with
  cooked-yield factors for display only.
- **Honest leftovers list.** Foods that didn't make the basket are shown in
  three structurally derived categories: can't use (dietary restrictions), no
  reliable price found, or simply not selected this time.

Each day on the Plan page also gets a nutrition panel comparing the day's
scheduled meals against household targets.

## The pantry & tracking loop

RightMeal tracks what you have at home and plans around it:

1. **Log purchases.** Tick *Purchased* on a basket line, use the direct-buy
   button, or use the camera: photograph a **product** or a whole **receipt**
   and OpenAI extracts observable facts only. It never receives the food
   catalog and never chooses a catalog ID. A deterministic local matcher uses
   identity-safe Pantry aliases, the bundled multilingual ONNX model, and hard
   form checks before showing candidates and match scores. Nothing is saved
   until you confirm, and prices are **never** invented. Confirmed catalog
   purchases become immutable log records (the source of truth) and their grams
   land in **My Pantry**. Foods with no safe mapping can remain planning-inert
   in **Custom Pantry** until you explicitly link them.
2. **Undo is exact.** Unticking a purchase voids the exact recorded event
   group — it never touches stock you entered or edited yourself.
3. **The next plan spends the pantry first.** Pantry stock enters the
   optimizer as free supply (capped at what you have and at the same per-food
   and food-group limits purchases face), so the budget goes only to what's
   missing. On the Plan page the ingredient split between *Use from pantry*
   and *Need to buy* is re-derived live from the current pantry every time the
   page renders — editing the pantry updates the shopping list without
   re-running the optimizer or changing any meal. The budget bar counts real
   purchases only. Finished (historical) plans keep their frozen snapshot.
4. **Cook and eat.** Marking a meal *Eaten* deducts its ingredients from the
   pantry (clamped to stock, batch dinners deduct two servings at once;
   leftover lunches deduct nothing). "Prepared" (inventory was deducted) and
   "eaten" (what the card shows) are tracked as separate facts, so correcting
   a mis-click can never deduct twice, and undo restores stock exactly.
5. **Leftovers become meals.** Add a one-sentence note in any language
   ("剩了大概三分之一", "we ate all the rice but left the chicken") and the AI
   estimates what's left — overall and per ingredient — or use the manual
   percentage dialog. Saved leftovers are stored as prepared food; when you
   build the next plan, a pre-pass pins substantial leftovers (roughly 60% of
   a slot's calories and half its protein) into upcoming lunch/dinner slots as
   ready meals and subtracts their nutrition from what needs to be bought.
   Leftovers are consumed only when actually eaten — regenerating a plan
   never uses anything up.

Generating a plan never silently consumes pantry stock or leftovers: inventory
changes only on a confirmed purchase, an explicit *Eaten*, or a pantry edit.

## AI features in detail (all optional)

All four AI features use the OpenAI API with strict JSON schemas and local
validation; anything malformed is rejected rather than trusted.

- **Plan explanations** — a plain-English summary of why the basket looks the
  way it does. Falls back to built-in local templates with no key or on any
  failure, so an explanation always renders.
- **Cooking steps** — 3–8 short numbered steps for a meal, restricted to the
  meal's actual ingredients and the household's dietary rules. Steps are
  AI-generated reference suggestions, not tested recipes. Results are
  cached on disk keyed by a versioned signature of the meal's facts, so each
  recipe is generated once and then renders instantly and offline. Steps are
  display-only text — they never touch inventory or the plan.
- **Photo & receipt purchase entry** — vision analysis reads *only what is
  visible*: product/brand text, food form, package sizes, quantities, source
  coordinates, and eligible printed item totals. Unreadable values stay empty;
  a price is never guessed. Receipt confirmation is blocked unless local
  coverage checks pass for line count, ordering, bounds, bottom coverage, and
  source resolution. One image is limited to 30 merchandise lines; longer
  receipts use 2-3 overlapping segment photos. Possible duplicate extractions
  are shown and never silently deleted. Catalog retrieval, scoring, form
  filtering, weight conversion, and explanations all happen locally.
- **Leftover estimates** — converts a free-form note into a leftover fraction
  (overall and per named ingredient). On any failure the manual percentage
  dialog opens instead; the estimate itself never mutates inventory directly.

## Reliability & data integrity

- **Multi-file transactions.** A tracking action can touch the plan, the
  pantry, and the leftovers file at once; all writes go through a shared
  transaction manager with a rollback journal, so the group lands entirely or
  not at all — even across a crash. Interrupted transactions are rolled back
  automatically on the next launch.
- **Atomic writes everywhere.** Every file save writes a temp file, flushes
  it to disk, and atomically replaces the original, so power loss can never
  leave a torn file.
- **The purchase log is never silently lost.** A corrupted purchase file is
  treated as an error, not an empty log: purchase features pause and the file
  is left untouched for recovery. Derived caches (like recipes) may legally
  reload empty; the source of truth may not.
- **In-memory rollback.** Every tracking operation snapshots state first and
  restores it if the save fails, so the UI never shows changes that didn't
  persist.
- **Offline-friendly images.** Meal photos are downloaded once, validated as
  real images, and served from a local cache forever after. Imported images are
  decoded with pinned Pillow, orientation-corrected, stripped of EXIF, and
  re-encoded before hashing or persistence. Custom Pantry can use an uploaded
  product image or a user-selected Wikimedia Commons result whose source,
  author, and license are retained.
- **Deterministic core.** Optimizer and scheduler contain no randomness; two
  runs on identical inputs produce identical plans.

## Privacy & local data

Everything is stored **locally only** — no accounts, no cloud sync — under
`%APPDATA%\RightMeal` on Windows (or `~/.rightmeal` elsewhere):

| File / folder | Contents |
|---|---|
| `profile.json` | Household, restrictions, allergies, location, optional API keys |
| `plan.json` | The saved meal plan and its basket snapshot |
| `pantry.json` | Current at-home stock |
| `leftovers.json` | Prepared-leftover records |
| `purchases.json` + `purchase_photos/` | Purchase log and its photos |
| `photo_imports.json` + `imported_images/` | Photo-import idempotency ledger and sanitized imported images |
| `recipes.json` | Cached AI cooking steps |
| `image_cache/` | Downloaded food photos |
| `tx_journal.json` | Transaction rollback journal (normally empty) |

Delete the folder — or use the *Delete saved data* button on the Profile
page — to remove everything. Keys entered in-app are saved in that local file
in plain text; prefer the `.env` file if that concerns you. Data leaves your
machine only for the explicit actions listed under *AI features* above and
for price lookups (which send the food name and your ZIP-derived location to
the pricing APIs). Product and receipt images are sent to OpenAI only after the
pre-selection disclosure; crop sensitive receipt content first because visual
card/member details cannot be reliably redacted automatically. Raw model
responses, card digits, member numbers, names, addresses, QR contents, and raw
transaction tokens are not persisted.

## Architecture

The codebase is layered, with strict one-way dependencies and no framework
code below the UI:

- **Models** — frozen dataclasses and enums for foods, nutrients, price
  quotes, baskets, meals, plans, pantry, leftovers, purchase records, and the
  household profile. Serialized plans store food ids and grams only; nutrients
  are always recomputed on load, never trusted from disk.
- **Data** — the curated 53-food catalog, the BLS price mapping, and nutrient
  targets as validated JSON, shipped inside the package so installers work
  with no extra configuration.
- **Services** — the price providers and fallback engine, unit normalization,
  name matching, session cache, nutrition math, the JSON stores, the
  transaction manager, the image cache, the four OpenAI clients, and the pure
  domain flows for purchases and meal tracking (UI handlers only call these
  and re-render; they never touch stores directly).
- **Optimizer** — hard-exclusion filters, scoring, greedy growth, repair, and
  bounded local search.
- **Planner** — meal templates, the leftover pre-pass, the day-by-day
  scheduler, and the unused-food categorizer.
- **UI** — Flet views for the five pages plus shared components, in a white /
  light-green / light-yellow theme.

## Tests

```bash
uv run pytest -q
```

531 tests cover the price fallback chain, BLS mapping
rules, unit normalization, allergy hard exclusions, the $50-week Los Angeles
family acceptance case, low-budget honesty, determinism, every explanation
fallback mode, meal-plan conservation (meals + carryover = basket exactly),
template structural rules, the leftover pre-pass, the pantry loop (an empty
pantry is bit-identical to no pantry; purchase-void and consumption-undo
restore stock exactly), evidence-only request boundaries, deterministic local
matching and form safety, receipt coverage and duplicate rules, image
sanitization, weight/price resolution, photo-import idempotency, Wikimedia
validation, transaction rollback and crash recovery, and store corruption
handling. No test touches the network — all HTTP is mocked.

## Building installers

See the [Flet packaging guides](https://flet.dev/docs/publish/) —
`flet build windows|macos|linux|web|apk|ipa`. The curated data ships inside
the package and is read relative to the module, so packaged builds work
without extra configuration.

## Resources

Data & methodology:

- USDA Thrifty Food Plan (structure inspiration): https://www.fns.usda.gov/cnpp/thrifty-food-plan-2021
- Dietary Guidelines for Americans: https://www.dietaryguidelines.gov/
- USDA FoodData Central: https://fdc.nal.usda.gov/api-guide
- NIH Dietary Reference Intakes: https://ods.od.nih.gov/HealthInformation/nutrientrecommendations.aspx
- BLS Average Prices: https://www.bls.gov/cpi/factsheets/average-prices.htm
- FAO/WHO Sustainable Healthy Diets: https://www.who.int/publications/i/item/9789241516648

APIs:

- Kroger API: https://developer.kroger.com/
- Instacart Developer Platform: https://docs.instacart.com/developer_platform_api
- OpenAI API: https://platform.openai.com/docs
- BLS Public Data API: https://www.bls.gov/developers/

Tooling:

- Flet (Python UI framework): https://flet.dev/docs/
- uv (Python package manager): https://docs.astral.sh/uv/
- httpx: https://www.python-httpx.org/
- pytest: https://docs.pytest.org/
