# RightMeal

<p align="center">
  <img src="RightMeal%20logo.png" alt="RightMeal logo" width="180">
</p>

RightMeal is a local-first grocery, meal, pantry, and nutrition-planning application built with Python and [Flet](https://flet.dev/). It creates a calendar of traceable, named recipes for a household, derives the ingredient demand, uses current pantry stock first, rounds the remaining demand into purchasable packages, and checks the resulting estimate against an **Estimated basket budget cap**.

The application also closes the planning loop: purchases can be recorded manually or from product/receipt photos, cooked meals can be tracked, leftovers can become future meals, and the next plan is built around what is still available.

RightMeal is a planning aid, not a store or checkout service. It has no accounts, carts, affiliate links, or purchase buttons that leave the application.

> **Recipe source:** The recipe markdown and corresponding finished-dish images are derived from [ronaldl29/public-domain-recipes](https://github.com/ronaldl29/public-domain-recipes), whose upstream repository releases its content into the public domain under the Unlicense. See [Sources and attribution](#sources-and-attribution).

## Table of contents

- [What RightMeal does](#what-rightmeal-does)
- [Important limitations](#important-limitations)
- [Quick start](#quick-start)
- [Configuration and API keys](#configuration-and-api-keys)
- [Using the application](#using-the-application)
- [How planning works](#how-planning-works)
- [Pricing and package selection](#pricing-and-package-selection)
- [Pantry, purchases, and leftovers](#pantry-purchases-and-leftovers)
- [Product-photo and receipt imports](#product-photo-and-receipt-imports)
- [Optional AI features](#optional-ai-features)
- [Nutrition model](#nutrition-model)
- [Privacy and local data](#privacy-and-local-data)
- [Reliability and data integrity](#reliability-and-data-integrity)
- [Architecture](#architecture)
- [Recipe catalog development](#recipe-catalog-development)
- [Testing](#testing)
- [Building packages](#building-packages)
- [Troubleshooting](#troubleshooting)
- [Sources and attribution](#sources-and-attribution)

## What RightMeal does

- Builds breakfast, lunch, and dinner schedules from real recipe records, never fabricated ingredient-name combinations.
- Keeps each scheduled meal traceable to its source markdown file and displays the original recipe directions when available.
- Supports household profiles containing adults, children, and seniors, plus vegetarian, no-pork, lactose-free, and free-form allergy settings.
- Applies dietary and allergy rules as hard filters before recipe scoring.
- Supports high-variety, balanced, and meal-prep planning styles.
- Uses a deterministic, budget-aware bounded beam search; identical durable inputs produce identical core planning results.
- Derives the shopping basket from the selected recipes instead of selecting groceries first and trying to invent meals afterward.
- Uses pantry stock and substantial prepared leftovers before adding purchases.
- Keeps retailer/package offers distinct and selects a minimum-cost combination of whole packages for each ingredient gap.
- Preserves price source, store, matched product, confidence, package, and cost information in the saved plan.
- Reports typed planning outcomes instead of presenting bounded-search exhaustion as proof that no plan exists.
- Tracks purchased food, prepared meals, eaten status, raw pantry deductions, and prepared leftovers.
- Supports manual pantry entries, inert custom items, product-photo imports, and multi-image receipt imports.
- Works without API keys; optional credentials improve live prices and enable OpenAI-assisted features.
- Stores ordinary user data locally and uses atomic, journaled transactions for multi-file state changes.

The current checked-in data contains:

| Data set | Current size | Runtime role |
|---|---:|---|
| Food catalog | 72 foods | Nutrition, dietary metadata, package options, prices, pantry, and matching |
| Seed catalog | 53 foods | Base reviewed catalog and local price fallback |
| Reviewed extended catalog | 19 foods | Additional reviewed foods, including USDA-assisted entries |
| Source recipe markdown | 408 recipes plus `content/_index.md` | Read-only recipe source corpus |
| Compiled recipe index | 408 recipes | Runtime recipe catalog |
| Auto-plannable recipes | 99 | 24 breakfast recipes and 75 lunch/dinner recipes |
| Bundled finished-dish images | 137 files | Offline recipe-card imagery when available |

These figures describe the current repository and will change as the catalog is reviewed and rebuilt.

## Important limitations

- **Not medical or dietary care.** RightMeal does not diagnose, treat, or prevent disease. Its targets are simplified, gender-averaged planning values. Consult a qualified professional for medical nutrition needs.
- **Not the USDA Thrifty Food Plan.** The project borrows ideas such as household targets, food-group reporting, and affordability analysis, but it does not reproduce or claim compliance with the official USDA TFP.
- **The budget is an estimate cap, not a checkout guarantee.** A basket can combine retailer quotes, regional averages, and local estimates. Tax, delivery charges, membership conditions, minimum orders, substitutions, availability, and later price changes are not included.
- **The search is bounded.** The planner is deterministic, but its beam width and per-slot candidate count are finite. Failure to find an in-cap plan is not mathematical proof that none exists.
- **A partial food-coverage plan is intentionally incomplete.** It always displays: "This is not a complete food plan. Additional food is required." It must be confirmed explicitly before it can be saved.
- **Dietary filtering depends on curated metadata.** The filters cannot guarantee manufacturing cross-contact, restaurant preparation, brand-specific formulations, or the completeness of upstream recipe text. Always inspect labels and recipe ingredients yourself.
- **Nutrition and price coverage are only as good as the reviewed mappings.** Unresolved ingredients are not assigned invented nutrition or prices.
- **AI output is unverified.** Model-generated explanations, fallback cooking steps, leftover estimates, and image extraction can be wrong. Local validation rejects many unsafe or malformed outputs, but user review remains required.
- **Receipt prices are evidence-only.** RightMeal accepts eligible positive printed merchandise totals; it does not infer missing prices or treat a receipt subtotal as an item price.
- **U.S.-oriented pricing.** The planning form currently requires a five-digit U.S. ZIP code, and the included BLS mapping is based on U.S. regions.

## Quick start

### Requirements

- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/) for environment and dependency management
- A supported desktop environment, or a modern browser for Flet web mode
- Platform toolchains only if you intend to build an installer or mobile package

### Install and run

```bash
git clone https://github.com/ShuaiLn/RightMeal.git
cd RightMeal
uv sync --all-groups
uv run flet run
```

Run the browser version instead:

```bash
uv run flet run --web
```

The Flet application entry point is `src/main.py`; bundled assets are served from `src/assets/`.

### Optional environment file

RightMeal loads `.env` automatically at startup. Copy the example and add only the keys you want to use.

PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS/Linux:

```bash
cp .env.example .env
```

Do not commit `.env` or real credentials.

## Configuration and API keys

All keys are optional. A value saved on the Profile page takes precedence over the matching environment variable.

| Variable | Purpose | Runtime required? |
|---|---|---|
| `KROGER_CLIENT_ID` | Kroger OAuth client ID for nearby product prices | No |
| `KROGER_CLIENT_SECRET` | Kroger OAuth client secret | No |
| `INSTACART_API_KEY` | Instacart product search and usable numeric prices | No |
| `BLS_API_KEY` | Higher BLS Public Data API limits | No; BLS also supports unregistered requests |
| `OPENAI_API_KEY` | Explanations, fallback cooking steps, leftover-note analysis, and photo/receipt extraction | No |
| `FDC_API_KEY` | Developer-only USDA FoodData Central candidate import | No; never used by the packaged runtime |

Without retailer credentials, RightMeal tries explicitly mapped BLS averages. If a required food still has no usable offer, the planner can retry that specific blocking food with the local seed estimate. The fallback is recorded and disclosed; it never replaces an available live or BLS offer.

Keys entered in the Profile page are stored as plain text in the local `profile.json`. Use environment variables or `.env` instead if that is preferable for your threat model.

## Using the application

### First launch

The onboarding form collects:

1. Household counts for adults, children, and seniors.
2. Vegetarian, no-pork, and lactose-free restrictions.
3. Optional free-form allergies.
4. A default city and five-digit U.S. ZIP code.

The profile can be edited later. If the household profile changes after a plan is saved, the plan is retained but marked stale so the user can decide when to regenerate it.

### The five pages

| Page | Purpose |
|---|---|
| **Start** | Set the Estimated basket budget cap, daily/weekly cap mode, ZIP code, inclusive date range, and variety style. Generate or delete the current plan. |
| **Plan** | Review the live pantry/buy allocation, package-level price sources, estimated cap status, daily recipe cards, directions, nutrition coverage, purchase groups, tracking, and explanation. |
| **Pantry** | Edit raw catalog stock, add items manually, import a product photo or receipt, manage custom items, link a custom item to the catalog, and manage prepared leftovers. |
| **Calendar** | View the saved plan on real calendar dates, inspect a day's meals and eaten-status dots, and jump back to that date in Plan. |
| **Profile** | Edit household settings and optional keys, inspect provider configuration, or clear all local user data after confirmation. |

### Typical workflow

1. Complete onboarding or update the household profile.
2. On Start, choose a cap, date range, ZIP code, and variety style.
3. Select **Plan my groceries**. RightMeal looks up offers, assigns usable prepared leftovers, searches the recipe catalog, and derives the basket.
4. Review the result on Plan. An ordinary complete in-cap plan is saved automatically; a partial plan requires two-step confirmation.
5. Record purchases from a basket line, a product photo, a receipt, or manual pantry entry.
6. Open a meal card to read its source directions and mark it eaten.
7. Record any leftovers manually or with an optional note analysis.
8. Generate the next plan. Available raw pantry stock and eligible prepared leftovers are considered before new purchases.

### Planning outcomes

The orchestration layer returns explicit outcome types:

| Outcome | Meaning | Saves automatically? |
|---|---|---:|
| `StandardPlanReady` | A complete, fully priced candidate fits the current estimated cap | Yes |
| `PartialFoodCoverageCandidate` | An explicitly incomplete candidate fits the cap and satisfies the partial-plan minimums | Only after explicit confirmation |
| `BudgetChoiceRequired` | A complete candidate was found above the cap; it is not claimed to be the minimum possible cost | No |
| `NoPlanFoundWithinSearchLimits` | The bounded search did not find a complete candidate | No |
| `DataUnavailable` | Required ingredient, catalog, package, or price evidence is missing | No |
| `NoFeasiblePlanProven` | Reserved for an exhaustive search or verified proof certificate; ordinary beam search never returns this as a proof | No |

Non-saveable outcomes leave any previously saved plan untouched.

## How planning works

RightMeal is **recipe-first**. The retired basket-first/template engine is not part of the current application.

```text
Recipe markdown + curated mappings
                |
                v
       Compiled recipe index
                |
                v
Profile + dates + pantry + prepared leftovers + package offers
                |
                v
Hard dietary/data gates -> deterministic recipe search -> validation/repair
                |
                v
        Breakfast/lunch/dinner plan
                |
                v
Sum ingredient demand -> subtract pantry -> optimize whole packages
                |
                v
Typed outcome -> validated saved snapshot -> live tracking/allocation
```

### 1. Compile the recipe catalog

At development time, `scripts/build_recipe_index.py` reads `content/*.md` and produces `src/data/recipe_index.json`. The runtime loads the compiled JSON; it does not parse hundreds of markdown files on startup.

The compiler performs these steps:

1. Parse front matter, servings, preparation/cooking time, ingredient sections, directions, and image references.
2. Parse quantities, units, ingredient names, preparation states, optional markers, and non-food equipment/material lines.
3. Resolve ingredients using reviewed catalog names, aliases, registry entries, and recipe/line overrides.
4. Classify ingredient roles such as protein, main carbohydrate, vegetable, fruit, dairy, fat, sauce, seasoning, and non-food.
5. Classify recipe type, eligible meal slots, cuisine, dish category, cooking methods, and batch suitability.
6. Compute per-serving nutrition only from ingredients that map to catalog foods with reviewed nutrition.
7. Mark a recipe `auto_plannable` only when it has a meal slot, all core ingredients are covered, at least 90% of relevant ingredient mass has reviewed nutrition, no substantial unresolved core ingredient remains, and computed calories are positive.
8. Emit coverage and unresolved-ingredient reports for human review.

Unresolved or pending ingredients are never assigned invented nutrition. Seasonings and optional/non-food lines are retained for display or classification but do not silently become inventory demand.

### 2. Assign prepared leftovers

Before searching new recipes, a deterministic pre-pass considers prepared-leftover records:

- Only available, unexpired records with valid dates are considered.
- They can fill lunch or dinner, never breakfast.
- The earliest use-by date wins, followed by preparation date and stable ID.
- A leftover must provide roughly 60% of that slot's calorie target and 50% of its protein target.
- An assigned leftover occupies its actual slot during search and creates no raw ingredient demand.
- Planning only reserves it; the record is consumed when the user marks that meal eaten.

### 3. Apply hard eligibility gates

The planner excludes recipes that:

- are not auto-plannable primary recipes;
- do not support the requested meal slot;
- conflict with vegetarian, no-pork, lactose-free, or matching allergy metadata;
- reference invalid provenance or foods outside the recipe/side definition;
- have severe per-person portion anomalies;
- violate structural rules such as an unapproved second main carbohydrate.

Dietary rules are not scoring penalties and cannot be traded for lower price or higher nutrition.

### 4. Score eligible recipes

The default soft score rewards:

- calorie fit for the meal slot;
- coverage by the pantry available at that point in the schedule;
- shorter total preparation/cooking time;
- a cuisine not recently used.

It penalizes:

- repeated recipes;
- recently repeated main proteins or main carbohydrates;
- high structural similarity to another recipe on the same day.

The current default heuristic weights are defined by `RecipePlanConfig`:

| Signal | Default weight/penalty |
|---|---:|
| Nutrition fit | `+1.0` |
| Pantry coverage | `+0.6` |
| Time score | `+0.2` |
| New-cuisine bonus | `+0.3` |
| Recipe repetition | `-1.5` per prior use |
| Same-day similarity | `-1.2 x similarity` above the threshold |
| Protein repetition | `-0.5` |
| Main-carbohydrate repetition | `-0.4` |

These are project heuristics, not scientific or regulatory scores.

### 5. Run deterministic budget-aware beam search

The scheduler fills slots chronologically. By default it retains at most 32 states and expands at most 24 candidates per parent/slot. Candidate selection deliberately reserves room for both quality-leading and exact-cost-leading states, rather than ranking everything by one blended dollars-and-quality number.

At every expansion, affected ingredient demand is repriced against the cumulative plan so whole-package boundaries are respected. Stable IDs and signatures break ties; there is no randomness or wall-clock cutoff in the core search.

The selected variety style changes repetition behavior:

| Style | Intended behavior |
|---|---|
| **High variety** | Prefer each recipe at most once in the strict pass |
| **Balanced** | Avoid using the same recipe on adjacent days |
| **Meal prep** | Permit more reuse and mark suitable dinners for batch preparation |

If the strict pass has no fully priced in-cap candidate, the scheduler runs one additional bounded rolling-seven-day pass. Actual candidate and pruned-state counts are attached to the outcome, but the search is still reported as non-exhaustive.

### 6. Repair meals without fabricating food

If a day is short on its calorie target, the scheduler may add a verified side recipe. It never tops a day up with arbitrary rice, oil, or other loose ingredients.

If the completed plan is over the estimated cap, a bounded budget-repair pass examines expensive swappable meals and tries cheaper same-slot recipes. A swap must:

- pass the complete recipe, dietary, portion, variety, and structural checks;
- not worsen the day's calorie-tolerance violation;
- stay within the configured quality-regression limit;
- reduce exact cumulative package cost by at least one cent;
- not introduce or grow an unpriced ingredient gap.

Batch meals, prepared leftovers, and leftover meals are not swapped. The repair is greedy and cannot guarantee a globally cheapest plan.

### 7. Derive the basket from the final plan

The final meal plan is converted into ingredient demand:

```text
food demand = sum of all raw/purchased-basis meal draws
purchase gap = max(0, food demand - available pantry grams)
```

Prepared-leftover meals draw no raw ingredients. Batch dinners account for their batch multiplier once, and the linked leftover lunch does not double-count the original ingredients.

The basket builder then chooses whole packages for every positive purchase gap. The saved result includes pantry use, package rows, nutrient totals, food-group coverage, gaps, unpriced foods, and fallback disclosures.

### 8. Optional partial food coverage

When a complete candidate is above the cap, RightMeal can search for an explicitly incomplete version of that same household/date plan:

- The complete household, every date, and all three daily meal slots remain.
- Every meal on the same day uses the same portion scale.
- Scales move in one-percentage-point steps.
- Every day must reach at least 60% of the household calorie target and 60% of the protein target.
- A water-filling phase raises the worst-covered day first.
- A second phase spends affordable package steps according to limiting-nutrition gain per cent.
- The result must fit the cap, have no unpriced purchase gaps, and obey a maximum of two recipe uses per rolling week, relaxed only to three when necessary.
- Remaining budget and the cost of the next unavailable 1% step are shown when known.

A partial plan does not claim micronutrient adequacy and cannot be saved without explicit confirmation.

## Pricing and package selection

### Offer tiers

The package-offer API uses tiers, not a simple "first quote wins" chain:

1. **Live retailer tier:** usable Kroger and Instacart offers may both contribute.
2. **BLS tier:** used only when no usable live offer exists for that food, and only for foods with an explicit mapping in `bls_price_map.json`.
3. **Local seed tier:** used only when the caller identifies a missing food as blocking planning.

Live retailer offers remain tied to the observed package. A normalized BLS estimate is projected onto the food's curated package sizes. Seed fallback exposes all positive curated package estimates. A lower tier never overwrites an available higher-tier offer.

### Quote safety rules

- Provider matches below `0.65` confidence are rejected.
- Kroger requires a positive numeric regular or promotional price and parseable package size.
- Instacart requires both a plain positive numeric price and a parseable size; ranges, missing prices, and size-free "each" records are rejected.
- BLS is limited to explicit food-to-series mappings and falls back from a regional series to the U.S. city average when needed.
- Mass and volume are normalized across grams, kilograms, ounces, pounds, milliliters, liters, fluid ounces, gallons, pints, quarts, counts, and dozens where sufficient catalog evidence exists.
- A volume-to-mass conversion requires a reviewed density; density is never guessed.
- Provider successes and failures are cached per session by provider, location, food, and parameters.
- At most four food price lookups run concurrently.

### Whole-package optimization

For one food, the basket builder uses a deterministic dynamic program over the available package offers. It selects the combination in this order:

1. Lowest total integer-cent cost.
2. Lowest excess weight/waste.
3. Fewest packages.
4. Stable expanded offer-ID order.

The budget status is then one of:

- `WITHIN`: all purchase gaps are priced and the estimate is within the cap;
- `OVER`: known cost already exceeds the cap;
- `UNKNOWN`: known cost is not over, but at least one required purchase gap is unpriced.

A known overage takes precedence over an unknown-price warning because missing prices can only increase the real total.

## Pantry, purchases, and leftovers

### Raw pantry

`My Pantry` stores catalog food IDs and grams. It can be edited directly, increased by purchases, and decreased when a normal meal is prepared. During generation, its stock is free supply.

For an active or future saved plan, the Plan page re-derives sourcing every time it renders:

```text
remaining requirement = frozen meal requirement - already prepared requirement
from pantry           = min(remaining requirement, current stock)
gap                    = max(0, remaining requirement - current stock)
```

The gap is refitted into the saved package offers. Pantry edits therefore change **Use from pantry** and **Need to buy** without changing the saved recipes. Plans whose end date is in the past use their frozen allocation snapshot instead.

### Custom items

Unmatched manual/photo/receipt items can be stored in `Custom items`. A custom item is deliberately inert: it does not affect planning, nutrition, package pricing, or catalog purchases until the user links it to a catalog food.

Custom items can retain an uploaded product image or a user-selected Wikimedia Commons image together with source, author, and license metadata.

### Purchase log

Confirmed catalog purchases create immutable purchase records. Purchase groups are the source of truth for:

- pantry additions;
- current-plan purchase progress;
- actual recorded spend;
- exact undo/void behavior;
- imported image references.

Undo voids the exact event group and reverses only the stock contributed by that group. It does not remove manual pantry edits or unrelated purchases.

### Meal tracking

For ordinary recipe meals, marking **Eaten** prepares the meal and deducts its raw ingredient draw from pantry, clamped at available stock. Preparation and display status are separate facts, so correcting a UI status cannot deduct the same ingredients twice.

Batch and prepared-leftover meals follow different accounting:

- A batch dinner can create a prepared-leftover record for a linked future lunch.
- The linked lunch consumes the prepared record and draws no raw pantry ingredients.
- A prepared-leftover meal uses its record, not the raw pantry.
- Undo is allowed only while downstream leftover history still makes an exact reversal safe.

### Leftovers

After eating a normal meal, the user can record a remaining fraction manually or describe it in a short note when OpenAI is configured. Component-specific fractions override the overall fraction. Saved leftovers keep per-food remaining grams, serving equivalents, preparation date, suggested use-by date, and status.

Planning never consumes a leftover merely by reserving it. It is reduced only after the corresponding meal is actually eaten.

## Product-photo and receipt imports

Photo features require an OpenAI key because the image itself is sent for structured visual extraction. Catalog matching, unit conversion, routing, duplicate checks, and persistence occur locally.

### Local image preprocessing

Before upload or persistence, Pillow:

- decodes the selected image;
- rejects empty, unreadable, non-positive, or over-40-megapixel sources;
- applies EXIF orientation;
- strips EXIF, ICC profiles, comments, and text metadata;
- re-encodes opaque images as JPEG and transparent images as PNG;
- rejects normalized output above 8 MB;
- hashes the sanitized bytes for stable duplicate detection.

### Product photo flow

1. The application discloses that the selected image will be sent to OpenAI.
2. OpenAI extracts observable facts such as visible name, brand, food form, quantity, and package weight.
3. A local matcher removes brand/package noise, applies exact Pantry aliases and hard food-form checks, and combines lexical matching with a bundled multilingual embedding model when available.
4. The user reviews the catalog match, weight evidence, package, destination, and any eligible price.
5. Only the confirmed command is committed.

### Receipt flow

- One receipt can use one to five images selected in top-to-bottom order.
- Each image receives one coordinate-free structured extraction request; the current scanner does not request OCR coordinates or receipt-boundary geometry.
- Lines are classified as food, non-food, discount, summary, or unknown.
- Purchased lines that the model reports as unreadable block the import rather than being guessed.
- Ordered overlap between adjacent segments is flagged for review; repeated real purchases are not silently deleted.
- Non-food, discount, and subtotal/total lines are never automatically added as food.
- Safe high-confidence items are preselected, but every detected line is shown in a single batch review. The user can uncheck or edit any item before saving.
- A catalog food still needed by the active plan defaults to Pantry/Plan progress. Other high-confidence food can default to Custom items; uncertain matches, weights, units, or destinations require editing.
- Only eligible positive printed merchandise line totals are recorded. Missing prices remain unknown.
- A final report lists Pantry additions, Custom-item additions, and every ignored/unconfirmed line.

Image hashes, transaction fingerprints, deterministic event IDs, optimistic revision checks, and an import ledger prevent accidental replay or stale-dialog commits. The plan, pantry, purchase log, import ledger, custom items, and image files are saved as one logical operation; failures roll back the JSON state and remove newly written images unless crash recovery is required.

Before selecting a receipt image, crop names, addresses, payment/member details, and QR codes. The application cannot reliably redact all sensitive visual content automatically.

## Optional AI features

The current OpenAI clients use `gpt-4o-mini` through the Chat Completions API with strict JSON schemas. A configured key does not allow the model to choose the plan, change inventory directly, or bypass the deterministic gates.

| Feature | Data sent | Local validation/fallback |
|---|---|---|
| Basket explanation | Verified basket facts, pantry use, household size, cap status, source labels, and nutrition gaps | Hallucinated foods, links, malformed JSON, or request failures fall back to deterministic local templates |
| Cooking steps | Meal name, verified core ingredient amounts, servings, and dietary restrictions | Original source directions are preferred. AI is used only when a meal has no source directions; invalid output becomes a placeholder |
| Leftover note | The note plus the meal's named ingredient IDs/amounts | Unknown IDs, malformed values, or failures are rejected; the UI keeps the manual slider available |
| Product/receipt analysis | The sanitized image selected by the user | Strict schema, observable-facts-only prompt, local catalog matching, user review, and evidence-only price rules |

Real recipe directions from `content/` are not AI-generated and are displayed with their `source_file`. AI fallback steps are cached in `recipes.json` by a versioned signature of the meal, quantities, servings, restrictions, and locale.

Without an OpenAI key:

- basket explanations use local deterministic templates;
- source recipe directions remain available;
- a meal without source directions shows a prompt to configure a key;
- leftover percentages can be entered manually;
- purchases can be entered manually, but product-photo and receipt analysis are unavailable.

## Nutrition model

RightMeal tracks 12 "more is better up to target" fields:

- calories;
- protein;
- fiber;
- calcium;
- iron;
- potassium;
- vitamin A;
- vitamin C;
- vitamin D;
- folate;
- magnesium;
- zinc.

Daily household targets are the sum of the configured adult, child, and senior planning values. Horizon targets multiply that daily total by the inclusive number of plan days.

Nutrition is computed on the catalog's purchased/raw basis, adjusted by edible fraction where defined. Dry-to-cooked yield is display-only and does not create additional nutrients or inventory. The Plan and Calendar views can compare scheduled or actually eaten meals with daily targets.

The application also reports six catalog food groups:

1. Grains and starchy foods
2. Protein foods
3. Vegetables
4. Fruits
5. Dairy and fortified alternatives
6. Healthy fats

The internal `nutrition_feasible` presentation flag currently means that no reported nutrient gap is below 50% of its horizon target; it does **not** mean every nutrient reaches 100%. Exact achieved/target gaps remain visible and are the authoritative report.

## Privacy and local data

Under the normal local desktop/web development run, RightMeal has no account or cloud-sync backend. User state is stored under:

- Windows: `%APPDATA%\RightMeal`
- macOS/Linux: `~/.rightmeal`

| File/folder | Contents |
|---|---|
| `profile.json` | Household, restrictions, location, variety mode, and optional in-app API keys |
| `plan.json` | Saved plan, meal schedule, package/offer snapshot, explanation, tracking, and coverage evidence |
| `pantry.json` | Catalog stock and custom items |
| `leftovers.json` | Prepared-leftover records |
| `purchases.json` | Immutable purchase/void history |
| `photo_imports.json` | Idempotency and image-reference ledger for photo imports |
| `recipes.json` | Derived cache of AI fallback cooking steps |
| `purchase_photos/` | Legacy/direct purchase photos when referenced |
| `imported_images/` | Sanitized product and receipt images retained by imports |
| `image_cache/` | Downloaded catalog food images |
| `matching_cache/` | Local catalog embedding cache; the model itself is bundled |
| `tx_journal.json` | Temporary rollback journal during a multi-file transaction |

The **Clear all user data** action removes the profile, plan, pantry, leftovers, purchase/import history, derived recipe cache, and stored user-photo files from that local profile directory. The current image and matching caches are local derived data and may also be removed manually by deleting the RightMeal directory.

Data can leave the machine in these cases:

- Kroger/Instacart price lookups send a food search term and ZIP-derived location.
- BLS requests send mapped time-series identifiers.
- OpenAI features send the facts or images listed above after the corresponding user action.
- Catalog ingredient images can be downloaded from TheMealDB and cached locally.
- A user-requested Custom-item image search contacts Wikimedia Commons.
- USDA FoodData Central is contacted only by an explicit developer tool, never by runtime planning.

If the Flet web application is deployed on a remote host, "local" means the filesystem of the Python application host, not necessarily the end user's browser device. Review the deployment architecture before exposing it to other users.

## Reliability and data integrity

- **Atomic files:** JSON stores use a temporary file and atomic replacement; transaction-managed files are flushed and `fsync`ed before replacement.
- **Journaled multi-file saves:** plan, pantry, leftovers, purchases, recipe cache, and photo-import ledger changes can be committed together. The pre-transaction contents are stored in `tx_journal.json` until commit.
- **Crash recovery:** startup checks the journal before loading stores. An interrupted transaction is rolled back; an unreadable journal is quarantined instead of ignored.
- **Single-process serialization:** a shared re-entrant lock prevents Flet handlers from interleaving writes. Cross-process writers are outside the design scope.
- **In-memory rollback:** stateful workflows snapshot live objects and restore them when persistence fails.
- **Purchase-log protection:** if an existing `purchases.json` is unreadable, purchase mutations and cleanup pause rather than treating history as empty.
- **Photo-ledger protection:** an unreadable photo-import ledger similarly pauses photo imports.
- **Optimistic revisions:** plan, pantry, purchase, and photo-import revisions invalidate stale confirmation dialogs.
- **Generation tokens:** a newer plan request, profile change, pantry change, or data clear invalidates an in-flight generation before it can save stale results.
- **Stable IDs:** plan, basket, purchase, custom-item, and photo-operation IDs make migrations and retries deterministic.
- **Derived data is recomputed:** saved plans store IDs and gram amounts; nutrition and other derived facts are validated or recomputed rather than blindly trusting arbitrary serialized totals.
- **Offline-friendly media:** bundled recipe photos and cached ingredient photos avoid repeated runtime downloads.

## Architecture

The project uses one-way layers. Flet-specific code stays in `src/ui/`; the planner and services can be tested without rendering controls.

```text
RightMeal/
|-- content/                    # read-only upstream recipe markdown
|-- src/
|   |-- main.py                 # Flet entry point and .env loading
|   |-- models/                 # dataclasses, enums, serialization, migrations
|   |-- data/                   # reviewed JSON and compiled recipe_index.json
|   |-- planner/                # recipe scheduler, validators, demand, leftovers
|   |-- services/               # pricing, stores, transactions, AI, matching, flows
|   |-- ui/                     # five pages, onboarding, dialogs, shared controls
|   `-- assets/                 # icons, logo, recipe images, bundled ONNX model
|-- scripts/
|   |-- recipe_indexer/         # markdown compiler pipeline
|   |-- build_recipe_index.py
|   |-- scan_ingredients.py
|   `-- fetch_recipe_images.py
|-- tools/                      # reviewed USDA import workflow
|-- reports/                    # catalog coverage and mapping diagnostics
|-- tests/                      # network-free pytest suite
|-- pyproject.toml
`-- uv.lock
```

### Models

Frozen dataclasses and enums represent foods, nutrients, price quotes/offers, basket rows, recipes, meals, planning outcomes, pantry/custom items, prepared leftovers, purchases, photo facts/imports, and saved plans. Store readers include schema-version checks and migrations for older records.

### Data

The runtime data layer validates the food catalog, all six food groups, package metadata, BLS mappings, nutrient targets, and recipe food IDs. Data paths are relative to the installed module so packaged builds do not depend on the working directory.

### Planner

`src/planner/recipe_scheduler.py` performs recipe filtering, scoring, bounded beam search, daily side repair, and whole-plan budget repair. `demand.py` derives ingredient demand. `partial_plan.py` constructs and validates explicit incomplete plans. `leftover_prepass.py` reserves substantial prepared meals. Validators and similarity logic remain pure domain code.

### Services

Services provide package pricing, dietary filters, nutrition math, live source allocation, pantry/purchase/meal-tracking flows, local JSON stores, transactions, image normalization/cache, local catalog matching, Wikimedia search, and the optional OpenAI clients.

### UI

`src/ui/app.py` owns application startup, recovery, navigation, and view switching. UI handlers call domain flows and then re-render; planning and import operations use snapshots/tokens so late async results cannot overwrite newer state.

## Recipe catalog development

The markdown under `content/` is treated as read-only source. Runtime changes appear only after rebuilding the compiled index.

### Rebuild the index

```bash
uv run python scripts/build_recipe_index.py
```

Outputs:

- `src/data/recipe_index.json`
- `reports/recipe_coverage_report.json`
- `reports/unresolved_ingredients_report.json`

Rebuild after changing recipe markdown or any compiler input, including the ingredient registry, ingredient aliases/overrides, ingredient portion defaults, recipe overrides, or the reviewed seed/extended food catalogs. The generated index and reports are tracked build products, so inspect their diff before committing.

### Review additional USDA foods

This is a human-review pipeline; candidates are never auto-promoted.

```bash
uv run python scripts/scan_ingredients.py 40
uv run python tools/import_usda_foods.py
uv run python tools/review_usda_mappings.py --list
uv run python tools/review_usda_mappings.py --decisions decisions.json
uv run python scripts/build_recipe_index.py
```

`FDC_API_KEY` is needed only for the candidate-fetch step. Approved mappings update `usda_food_mappings.json` and `extended_foods.json`; nutrition is reviewed before it becomes runtime data.

### Fetch bundled recipe images

```bash
uv run python scripts/fetch_recipe_images.py
```

The script downloads referenced finished-dish WebP images from the public-domain recipe origin, validates response type/size, and skips existing files.

### Optional live image check

```bash
uv run python scripts/check_food_images.py
```

Unlike the pytest suite, this manual check accesses TheMealDB over the network.

## Testing

Run the full suite:

```bash
uv run pytest -q
```

Collect tests without running them:

```bash
uv run pytest --collect-only -q
```

The current repository collects 676 tests across 51 test modules. Coverage includes:

- catalog validation and USDA review boundaries;
- ingredient parsing, aliases, roles, non-food detection, and recipe gates;
- recipe provenance, structure, nutrition, variety, determinism, and conservation;
- bounded beam-search outcomes, partial-plan fairness, and budget repair;
- price-provider fallback, confidence, units, package-offer optimization, and exact cents;
- pantry allocation, purchases, voids, meal preparation, batch meals, and leftovers;
- strict OpenAI request/response validation and local fallbacks;
- local multilingual matching, form conflicts, package units, and weight evidence;
- product/receipt image normalization, scanning, overlap, review, and idempotency;
- store migrations, corruption behavior, transaction rollback, and crash recovery;
- Flet UI logic and stale async-operation protection.

Tests do not contact real services. HTTP behavior uses mocks/`httpx.MockTransport`. No linter configuration is currently defined in `pyproject.toml`.

## Building packages

Flet 0.85.3 is locked in the current environment. Examples:

```bash
uv run flet build windows
uv run flet build macos
uv run flet build linux
uv run flet build web
uv run flet build apk
uv run flet build aab
uv run flet build ipa
```

See the [Flet publishing guide](https://flet.dev/docs/publish/) for required Flutter/platform toolchains, signing, permissions, and deployment. Default output is under `build/<target>/`.

Before distributing a package, replace the placeholder organization/company/author metadata in `pyproject.toml`, choose a stable bundle ID, review signing settings, and confirm the intended privacy model for web deployment.

## Troubleshooting

### Recipe or data changes are not visible

The application loads `src/data/recipe_index.json`, not live markdown. Rebuild the index and restart:

```bash
uv run python scripts/build_recipe_index.py
uv run flet run
```

### Planning reports missing data

`DataUnavailable` identifies whether the blocker is an ingredient mapping, catalog entry, package size, or price. Unused broken recipes can remain diagnostics, but missing evidence on the selected candidate blocks saving. Check the generated reports and package/price data rather than adding guessed values.

### A plan is above the cap

The displayed candidate is not claimed to be globally cheapest. Try a larger cap, a shorter range, a different variety style, additional pantry stock, or different retailer credentials. A qualifying partial plan may be offered, but it requires additional food and explicit confirmation.

### Pantry edits changed the shopping list but not the meals

This is expected. Meals are frozen in the saved plan; active-plan sourcing is recalculated from current pantry stock. Generate a new plan if you want the recipe schedule itself to change.

### Photo or receipt buttons say OpenAI is not configured

Add `OPENAI_API_KEY` to `.env` or the Profile page, then restart/reopen the flow. Manual pantry and purchase actions do not need OpenAI.

### Purchase actions are paused

RightMeal detected an unreadable purchase log or photo-import ledger and intentionally refused to treat it as empty. Back up `%APPDATA%\RightMeal` or `~/.rightmeal`, inspect the reported file, and restore a valid copy before retrying.

### Test with isolated local data

To avoid touching a real profile during manual verification, point `APPDATA` on Windows or the home directory used by the process on macOS/Linux to a scratch location before launch.

PowerShell example:

```powershell
$env:APPDATA = "$PWD\.tmp-appdata"
uv run flet run
```

### Reset everything

Use **Profile -> Clear all user data**, or close RightMeal and remove its local data directory manually. This cannot be undone.

## Sources and attribution

### Recipe corpus and dish images

- **Primary recipe source:** [ronaldl29/public-domain-recipes](https://github.com/ronaldl29/public-domain-recipes)
- The repository's recipe markdown is represented under `content/` and compiled into `src/data/recipe_index.json`.
- Corresponding upstream finished-dish images are bundled under `src/assets/recipe_images/` when available.
- The upstream project states that its website and content are in the public domain and includes an [Unlicense license](https://github.com/ronaldl29/public-domain-recipes/blob/main/LICENSE.md).
- Individual recipe author fields from the upstream markdown remain in the source files even though the current compiled runtime model does not expose every author field.

The upstream public-domain declaration applies to that upstream content. It does not automatically license unrelated RightMeal source code. This repository currently has no top-level `LICENSE` file; add one before assuming or publishing a license for the application code.

### Nutrition and methodology

- [USDA FoodData Central](https://fdc.nal.usda.gov/api-guide) - developer-assisted nutrition candidates; only reviewed mappings enter the catalog
- [USDA Thrifty Food Plan, 2021](https://www.fns.usda.gov/cnpp/thrifty-food-plan-2021) - structural inspiration only
- [Dietary Guidelines for Americans](https://www.dietaryguidelines.gov/) - general dietary reference
- [NIH Dietary Reference Intakes](https://ods.od.nih.gov/HealthInformation/nutrientrecommendations.aspx) - background for simplified planning targets
- [FAO/WHO Sustainable Healthy Diets](https://www.who.int/publications/i/item/9789241516648) - general methodology reference

### Prices and commerce data

- [U.S. Bureau of Labor Statistics Average Prices](https://www.bls.gov/cpi/factsheets/average-prices.htm)
- [BLS Public Data API](https://www.bls.gov/developers/)
- [Kroger Developer API](https://developer.kroger.com/)
- [Instacart Developer Platform](https://docs.instacart.com/developer_platform_api)

### Images and local matching

- [TheMealDB API](https://www.themealdb.com/api.php) - catalog ingredient image URLs
- [Wikimedia Commons API](https://www.mediawiki.org/wiki/API:Main_page) - user-selected licensed images for Custom items
- [sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) - multilingual matching model family
- [qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q](https://huggingface.co/qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q) - bundled FastEmbed ONNX source, revision recorded in `MODEL_SOURCE.md`, Apache-2.0 as reported there

### Application tooling and services

- [Flet](https://flet.dev/docs/) - Python UI framework
- [uv](https://docs.astral.sh/uv/) - dependency and environment management
- [httpx](https://www.python-httpx.org/) - async HTTP client
- [Pillow](https://pillow.readthedocs.io/) - local image decoding and sanitization
- [FastEmbed](https://github.com/qdrant/fastembed) - local ONNX text embeddings
- [OpenAI API](https://platform.openai.com/docs) - optional structured explanations and visual/text extraction
- [pytest](https://docs.pytest.org/) and [pytest-asyncio](https://pytest-asyncio.readthedocs.io/) - test suite
