# AGENT.md

Guidance for AI coding agents working in this repo. RightMeal is a local-only
Flet (Python) desktop/web app that plans a grocery basket and calendar of
named meals for a household within a budget. See [README.md](README.md) for
the user-facing feature description and disclaimers.

> **README.md describes the current recipe-first application.** Keep its
> complete feature inventory, limitations, architecture, and repository counts
> synchronized with behavior changes. When documentation and code conflict,
> verify the current implementation and update both this file and the README.

## Run & test

```bash
uv sync --all-groups     # create venv, install deps (Python 3.10+)
uv run flet run          # desktop app
uv run flet run --web    # browser
uv run pytest -q         # full test suite (no network — all HTTP is mocked)
```

Rebuilding the recipe catalog (dev-time only, not run by the app):

```bash
python scripts/build_recipe_index.py
```

Reads `content/*.md` (408 recipe files plus `_index.md`, read-only) plus the
curated JSON in `src/data/` (`ingredient_registry.json`,
`ingredient_aliases.json`, `ingredient_overrides.json`,
`ingredient_portion_defaults.json`, `ingredient_price_defaults.json`,
`portion_rules.json`, `recipe_overrides.json`), resolves and classifies every
ingredient line, and writes `src/data/recipe_index.json` (the compiled
artifact the app actually loads at runtime) plus coverage reports under
`reports/`. If you edit `content/*.md` or any of those curated JSON inputs,
re-run this before the app or tests will see the change — `recipe_index.json`
is checked in as a build product, not derived at import time.

No linter is configured (no ruff/flake8 config in the repo).

## Architecture (current)

Layered, one-way dependencies, no framework code below the UI:

- **`src/models/`** — frozen dataclasses/enums: `food.py`, `recipe.py`,
  `meals.py`, `plan.py`, `pantry.py`, `prepared_leftover.py`,
  `purchase_log.py`, `profile.py`, `basket.py`, `pricing.py`. Saved plans
  store ids/grams only; nutrients are always recomputed on load, never
  trusted from disk.
- **`src/data/`** — curated JSON (foods, ingredient registry/aliases,
  nutrient targets, BLS price map, portion rules) plus the compiled
  `recipe_index.json`, loaded through `data/loader.py`'s validating loaders.
- **`src/services/`** — price providers + fallback engine (`price_engine.py`,
  `price_providers/`), unit/name matching, `nutrition.py`, JSON stores
  (`profile_store.py`, `plan_store.py`, `pantry_store.py`,
  `prepared_leftovers_store.py`, `purchase_log_store.py`, `recipe_store.py`),
  `tx.py` (shared transaction manager), `image_cache.py`, the four OpenAI
  clients (`explanation/`, `photo_analyzer.py`), `basket_builder.py`,
  `source_allocation.py`, `dietary.py` (hard exclusions), and the pure
  domain flows `pantry_flow.py` / `meal_tracking_flow.py` (UI handlers call
  these and re-render; they never touch stores directly).
- **`src/planner/`** — the recipe-first plan generator: `recipe_scheduler.py`
  (filters/scores/validates real recipes into a `MealPlan`), `demand.py`
  (aggregates ingredient demand from the plan for the basket), plus
  `daily_validator.py`, `meal_validator.py`, `similarity.py`,
  `leftover_prepass.py`, `unused.py`.
- **`src/ui/`** — Flet views for the five pages (`start_view.py`,
  `planning_view.py`, `pantry_view.py`, `calendar_view.py`,
  `profile_view.py`) plus `app.py` (shell/nav), `state.py` (`AppState`,
  session-wide), `components.py`, `meals_section.py`, `photo_purchase.py`,
  `date_range_picker.py`.

### The recipe-first pivot

The old design built a shopping basket first
via a pure-Python optimizer (`src/optimizer/`, now deleted) against a curated
~53-food catalog, then scheduled hand-written dish *templates*
(`SHORT_NAMES`, `ComponentSpec.eligible`) on top of it. That engine is gone.

The current design inverts the flow (see `services/planner_engine.py`,
module docstring): real recipes parsed from `content/*.md` are filtered,
scored, and validated directly into a `MealPlan`
(`planner/recipe_scheduler.py::build_recipe_plan`); the shopping basket is
then *derived* from the plan's ingredient demand
(`planner/demand.py::ingredient_demand` → `services/basket_builder.py`).
`build_shopping_result` still returns an `OptimizationResult` so downstream
explanation/shopping-list/unused-food code keeps working unchanged. A meal
always references a real recipe id traceable to its `source_file` — nothing
is fabricated, and calories are never topped up with an arbitrary
rice/oil addition. If no valid plan can be built, `build_recipe_plan` raises
`PlanGenerationError` with concrete reasons rather than returning a bad plan.

Do not resurrect references to `optimizer/`, `templates.py`, or the old
template-meal `SHORT_NAMES` table. Those planning components no longer exist.
`planner.food_labels.SHORT_NAMES` is a current, presentation-only food-label
mapping and is unrelated to the retired template engine. If an old note
mentions template eligibility or basket-first planning, treat it as historical
and check current code instead.

## Invariants worth protecting

- **Deterministic core.** No randomness, no wall-clock cutoffs anywhere in
  `planner/` or `services/basket_builder.py`. Identical inputs must produce
  identical output — tests rely on this.
- **Hard exclusions vs. soft constraints.** Allergies and diet rules
  (`services/dietary.py`) are a hard gate applied before scoring, for both
  foods and recipes — never a scoring penalty, never traded off against
  price or nutrition.
- **Budget is a hard cap**; nutrition adequacy and variety are honestly
  reported as shortfalls, never silently dropped.
- **Atomic writes + one shared transaction manager.** Every store write goes
  through `services/tx.py`'s `TransactionManager` so a multi-file action
  (plan + pantry + leftovers) lands entirely or not at all, including across
  a crash (`tx_journal.json` recovery runs on next launch, before stores
  load — see `ui/app.py::main`). Don't write a JSON store directly; go
  through the existing store class.
- **Purchase log is a source of truth.** A corrupted `purchases.json` is
  treated as an error, not an empty log — purchase features pause rather
  than silently losing history. Derived caches (recipes/steps cache, image
  cache) may legally reload empty.
- **No network in tests.** All HTTP goes through `httpx`; tests use
  `httpx.MockTransport` (see `tests/conftest.py::openai_client` for the
  pattern). Don't add a test that hits a real API.

## Known gotchas

- **Flet `STRETCH` + unbounded height.** `ft.Row(vertical_alignment=STRETCH)`
  with `expand=True` children silently fails to render (no exception) inside
  any `Column`/`ListView` with `scroll=` set, because scroll gives children
  unbounded height and stretch needs a bounded cross-axis. Give the Row an
  explicit `height=`, or give children fixed heights instead.
- **`scroll_to` needs `ft.ScrollKey(...)`**, not a plain string `.key` —
  passing a bare string silently fails to scroll.
- **No `suffix_text` kwarg on `TextField`** in this Flet version (0.85) —
  use `suffix=ft.Text(...)`.
- **`FilePicker` as an overlay control** needs ≥2 outside strong references
  or it gets pruned from the control registry after any event and renders as
  a red "Unknown control".
- **Local data lives under `%APPDATA%\RightMeal`** (Windows) /
  `~/.rightmeal` elsewhere (`services/profile_store.py::default_profile_dir`).
  For manual verification without touching real user data, point `APPDATA`
  (or `HOME` on non-Windows) at a scratch directory before launching.

## Tests

`uv run pytest -q` — network-free, `pythonpath=src` (set in
`pyproject.toml`). Test files mirror `src/services/`, `src/planner/`, and
`src/models/` closely (e.g. `tests/test_recipe_engine.py`,
`tests/test_leftover_prepass.py`, `tests/test_tx.py`,
`tests/test_pantry_flow.py`); check for an existing test module before
adding a new one for a service you're touching.
