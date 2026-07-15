"""Fixture-based schema-migration tests.

Real old-schema JSON is built by hand and loaded through the current models, so
the migration paths are exercised end-to-end rather than by round-tripping the
current serializer. Covers:

- SavedPlan v2 -> v4 and v3 -> v4 (legacy template meals become legacy meals)
- Pantry v1 -> v2 (empty custom-item list)
- PreparedLeftover v1 -> v2 (portions default to the main component)
- Legacy-meal readability (nutrients still compute)
- MealPortion component-provenance migration (main/side preserved; absent = main)
- Purchased / Eaten / Undo / source allocation still work on a migrated plan
"""

from datetime import date

import pytest

from models import MealSlot, Pantry, SavedPlan
from models.meals import SOURCE_LEGACY, SOURCE_RECIPE
from models.pantry import MAPPING_PENDING, CustomPantryItem
from models.prepared_leftover import (
    COMPONENT_BOTH,
    COMPONENT_MAIN,
    COMPONENT_SIDE,
    component_summary,
)
from models.purchase_log import PurchaseInput, new_purchase_event_id
from services.meal_tracking_flow import (
    can_undo_preparation,
    prepare_and_eat,
    undo_preparation,
)
from services.pantry_flow import record_purchase_event
from services.prepared_leftovers_store import PreparedLeftoversStore
from services.source_allocation import allocate_sources

from test_meal_tracking_flow import StubState  # reuse the TrackingState stub

WHEN = date(2026, 7, 7)


# -- raw old-schema builders -------------------------------------------------


def _legacy_meal(slot, template_id, name, portions):
    """A v2/v3 meal dict: only template_id + bare portions (no provenance)."""
    return {
        "slot": slot,
        "template_id": template_id,
        "name": name,
        "is_leftover": False,
        "batch_id": None,
        "portions": [{"food_id": fid, "grams": g, "cooked_grams": None} for fid, g in portions],
    }


def _recipe_meal(slot, recipe_id, name, mains, sides=()):
    """A v4 recipe meal dict with component provenance on portions."""
    portions = [
        {"food_id": fid, "grams": g, "cooked_grams": None,
         "component_kind": "main", "source_recipe_id": recipe_id}
        for fid, g in mains
    ]
    side_rid = f"side-{recipe_id}" if sides else None
    portions += [
        {"food_id": fid, "grams": g, "cooked_grams": None,
         "component_kind": "side", "source_recipe_id": side_rid}
        for fid, g in sides
    ]
    return {
        "slot": slot, "template_id": "", "recipe_id": recipe_id, "source_kind": "recipe",
        "servings": 4.0, "side_recipe_id": side_rid,
        "side_servings": 4.0 if side_rid else 0.0, "name": name,
        "is_leftover": False, "batch_id": None, "portions": portions,
    }


def _raw_plan(version, foods_by_id, *, plan_id=None, meals=None, basket_food="rice_white"):
    day_meals = meals or [
        _legacy_meal("dinner", "grain_protein_bowl", "Chicken and rice",
                     [("rice_white", 300.0), ("chicken_breast", 360.0)]),
    ]
    pkg = foods_by_id[basket_food].package_options[0]
    data = {
        "version": version,
        "start_date": WHEN.isoformat(),
        "horizon_days": 7,
        "created_at": "2026-07-07T09:00:00",
        "budget": 60.0,
        "total_cost": 25.0,
        "basket": [{
            "food_id": basket_food, "package_label": pkg.label, "count": 2, "cost": 6.5,
            "source": "seed", "store": "Seed data", "confidence": 1.0,
            "match_reason": "seed", "matched_product_name": foods_by_id[basket_food].name,
        }],
        "meal_plan": {"horizon_days": 7, "pantry_carryover": {},
                      "days": [{"day_index": 0, "meals": day_meals}]},
        "consumed_gaps": [],
        "tracking": {},
        "purchased": {},
    }
    if plan_id:
        data["plan_id"] = plan_id
    return data


# -- SavedPlan v2 -> v4 ------------------------------------------------------


def test_plan_v2_migrates_to_v4(foods_by_id):
    plan = SavedPlan.from_dict(_raw_plan(2, foods_by_id), foods_by_id)
    assert plan is not None
    assert plan.needs_resave is True            # a resave will persist the upgrade
    assert plan.variety_mode == "balanced"      # v4 default
    assert plan.staples == ()
    meal = plan.meal_plan.days[0].meals[0]
    # A field named recipe_id only ever holds a real catalog id: legacy -> None.
    assert meal.recipe_id is None
    assert meal.source_kind == SOURCE_LEGACY
    assert meal.template_id == "grain_protein_bowl"
    # Legacy portions migrate to the main component with no source recipe.
    for portion in meal.portions:
        assert portion.component_kind == COMPONENT_MAIN
        assert portion.source_recipe_id is None


def test_plan_v2_plan_id_is_deterministic(foods_by_id):
    raw = _raw_plan(2, foods_by_id)  # no plan_id
    first = SavedPlan.from_dict(raw, foods_by_id)
    second = SavedPlan.from_dict(raw, foods_by_id)
    assert first.plan_id == second.plan_id  # derived, stable across retries
    assert first.plan_id  # non-empty


# -- SavedPlan v3 -> v4 ------------------------------------------------------


def test_plan_v3_migrates_keeping_plan_id(foods_by_id):
    plan = SavedPlan.from_dict(_raw_plan(3, foods_by_id, plan_id="keep-me"), foods_by_id)
    assert plan is not None
    assert plan.plan_id == "keep-me"
    assert plan.needs_resave is True  # v3 < v4
    assert plan.meal_plan.days[0].meals[0].source_kind == SOURCE_LEGACY


def test_legacy_meal_is_readable_after_migration(foods_by_id):
    """A migrated legacy meal still computes nutrients and has no per-person kcal
    (servings unknown) — the UI falls back to the household total."""
    plan = SavedPlan.from_dict(_raw_plan(2, foods_by_id), foods_by_id)
    meal = plan.meal_plan.days[0].meals[0]
    assert meal.nutrients.calories_kcal > 0
    assert meal.per_person_kcal is None  # servings == 0 for legacy


# -- MealPortion provenance migration ----------------------------------------


def test_recipe_meal_provenance_round_trips(foods_by_id):
    meals = [_recipe_meal("dinner", "r-1", "Chicken and rice with broccoli",
                          [("rice_white", 150.0), ("chicken_breast", 180.0)],
                          sides=[("broccoli_frozen", 110.0)])]
    plan = SavedPlan.from_dict(_raw_plan(4, foods_by_id, plan_id="p", meals=meals), foods_by_id)
    meal = plan.meal_plan.days[0].meals[0]
    assert meal.source_kind == SOURCE_RECIPE
    assert meal.recipe_id == "r-1"
    assert meal.side_recipe_id == "side-r-1"
    mains = meal.portions_for("main")
    sides = meal.portions_for("side")
    assert {p.food.id for p in mains} == {"rice_white", "chicken_breast"}
    assert {p.food.id for p in sides} == {"broccoli_frozen"}
    assert all(p.source_recipe_id == "r-1" for p in mains)
    assert all(p.source_recipe_id == "side-r-1" for p in sides)


def test_portions_without_provenance_default_to_main(foods_by_id):
    plan = SavedPlan.from_dict(_raw_plan(4, foods_by_id, plan_id="p"), foods_by_id)
    meal = plan.meal_plan.days[0].meals[0]
    assert meal.portions_for("main")  # bare portions load as main
    assert not meal.portions_for("side")


# -- Pantry v1 -> v2 ---------------------------------------------------------


def test_pantry_v1_loads_with_empty_custom_items(foods_by_id):
    raw = {"version": 1, "updated_at": "2026-07-07T00:00:00",
           "items": {"rice_white": 500.0, "chicken_breast": 200.0}}
    pantry = Pantry.from_dict(raw, foods_by_id)
    assert pantry.items == {"rice_white": 500.0, "chicken_breast": 200.0}
    assert pantry.custom_items == []


def test_pantry_v2_round_trips_custom_items(foods_by_id):
    original = Pantry(items={"rice_white": 100.0})
    original.add_custom_item(CustomPantryItem(
        id="custom:abc", original_name="marmite", display_name="Marmite",
        amount=1.0, unit="jar", grams_estimate=250.0, brand="Acme",
        price=4.5, expiration="2026-12-01", mapping_status=MAPPING_PENDING,
        created_at="2026-07-07T00:00:00",
    ))
    reloaded = Pantry.from_dict(original.to_dict(), foods_by_id)
    assert reloaded.to_dict()["version"] == 3
    assert len(reloaded.custom_items) == 1
    item = reloaded.custom_items[0]
    assert item.id == "custom:abc"
    assert item.grams_estimate == 250.0
    assert item.mapping_status == MAPPING_PENDING
    assert item.canonical_food_id is None


def test_custom_item_is_inert_until_linked(foods_by_id):
    pantry = Pantry(items={})
    pantry.add_custom_item(CustomPantryItem(
        id="custom:xyz", original_name="quorn", display_name="Quorn",
        amount=1.0, unit="pack", grams_estimate=300.0,
    ))
    # Pending: contributes nothing to the planning inventory.
    assert pantry.items == {}
    assert pantry.pending_custom_items()
    # Linking is the single moment its grams enter the inventory.
    assert pantry.link_custom_item("custom:xyz", "tofu_firm") is True
    assert pantry.items == {"tofu_firm": 300.0}
    assert pantry.pending_custom_items() == []
    # Idempotent: a second link never double-counts.
    assert pantry.link_custom_item("custom:xyz", "tofu_firm") is False
    assert pantry.items == {"tofu_firm": 300.0}


# -- PreparedLeftover v1 -> v2 -----------------------------------------------


def test_leftover_v1_loads_portions_as_main(tmp_path, foods_by_id):
    store = PreparedLeftoversStore(tmp_path)
    store.base_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "version": 1,  # pre-provenance file
        "updated_at": "2026-07-07T00:00:00",
        "items": [{
            "id": "lo1", "origin_kind": "user", "source_date": "2026-07-07",
            "source_slot": "dinner", "source_meal_template_id": "t",
            "meal_name": "Chicken and rice", "note": "",
            "initial_fraction_remaining": 1.0, "servings_remaining": 1.0,
            "portions": [
                {"food_id": "rice_white", "food_name": "White rice",
                 "original_grams": 200.0, "remaining_grams": 200.0},
                {"food_id": "chicken_breast", "food_name": "Chicken breast",
                 "original_grams": 180.0, "remaining_grams": 180.0},
            ],
            "prepared_at": "2026-07-07", "use_by_date": "2026-07-10",
            "created_at": "2026-07-07T20:00:00", "status": "available",
        }],
    }
    import json
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.load(foods_by_id)
    assert len(loaded) == 1
    lo = loaded[0]
    assert all(p.component_kind == COMPONENT_MAIN for p in lo.portions)
    assert all(p.source_recipe_id is None for p in lo.portions)
    assert component_summary(lo) == COMPONENT_MAIN


def test_leftover_v2_preserves_component_provenance(tmp_path, foods_by_id):
    from models.prepared_leftover import (
        PreparedFoodPortion,
        PreparedLeftover,
        refresh_derived_fields,
    )
    lo = PreparedLeftover(
        id="lo2", origin_kind="user", source_date="2026-07-07", source_slot="dinner",
        source_meal_template_id="", meal_name="Beef stew with rice", note="",
        initial_fraction_remaining=1.0, servings_remaining=0.0,
        portions=[
            PreparedFoodPortion("beef_stew_meat", "Beef", 200.0, 200.0,
                                component_kind=COMPONENT_MAIN, source_recipe_id="main-r"),
            PreparedFoodPortion("rice_white", "Rice", 150.0, 150.0,
                                component_kind=COMPONENT_SIDE, source_recipe_id="side-r"),
        ],
        prepared_at="2026-07-07", use_by_date="2026-07-10",
        created_at="2026-07-07T20:00:00", status="available",
    )
    refresh_derived_fields(lo, foods_by_id)
    store = PreparedLeftoversStore(tmp_path)
    store.save([lo])
    reloaded = store.load(foods_by_id)[0]
    by_food = {p.food_id: p for p in reloaded.portions}
    assert by_food["beef_stew_meat"].component_kind == COMPONENT_MAIN
    assert by_food["beef_stew_meat"].source_recipe_id == "main-r"
    assert by_food["rice_white"].component_kind == COMPONENT_SIDE
    assert by_food["rice_white"].source_recipe_id == "side-r"
    assert component_summary(reloaded) == COMPONENT_BOTH


# -- purchased / eaten / undo / source allocation after migration ------------


def test_purchase_eat_undo_and_allocation_after_migration(foods_by_id):
    meals = [_recipe_meal("dinner", "r-9", "Chicken and rice",
                          [("rice_white", 300.0), ("chicken_breast", 360.0)])]
    plan = SavedPlan.from_dict(
        _raw_plan(3, foods_by_id, plan_id="p-9", meals=meals), foods_by_id)
    assert plan is not None

    pantry = Pantry()
    # Source allocation renders a migrated plan without error.
    allocations = allocate_sources(plan, pantry, foods_by_id)
    assert "rice_white" in allocations
    assert allocations["rice_white"].meal_requirement == pytest.approx(300.0)

    # Purchased: grams flow into the pantry and the plan aggregate.
    log = []
    event_id = new_purchase_event_id()
    record_purchase_event(plan, pantry, log, PurchaseInput(
        event_id=event_id, food_id="rice_white", grams=1000.0,
        apply_to_plan=True, group_id=event_id))
    assert pantry.items["rice_white"] == 1000.0
    assert plan.purchased.get("rice_white") == 1000.0

    # Eaten: the meal draw is deducted from stock.
    state = StubState(foods_by_id, pantry=pantry)
    meal = plan.meal_plan.days[0].meal_for(MealSlot.DINNER)
    assert prepare_and_eat(state, plan, plan.start_date, MealSlot.DINNER, meal).ok
    assert pantry.items["rice_white"] == pytest.approx(700.0)  # 1000 - 300

    # Undo: exactly what was deducted comes back.
    assert can_undo_preparation(state, plan, plan.start_date, MealSlot.DINNER, meal)
    assert undo_preparation(state, plan, plan.start_date, MealSlot.DINNER, meal).ok
    assert pantry.items["rice_white"] == pytest.approx(1000.0)


def test_custom_item_can_never_be_purchased(foods_by_id):
    """A pending custom pantry id is not a catalog food and must be refused by
    the purchase path — it can never enter purchases or meal draws."""
    pantry = Pantry()
    log = []
    event_id = new_purchase_event_id()
    with pytest.raises(ValueError):
        record_purchase_event(None, pantry, log, PurchaseInput(
            event_id=event_id, food_id="custom:abc", grams=100.0,
            apply_to_plan=False, group_id=event_id))


# -- BudgetStatus migration (v4 boolean -> v5 enum) ---------------------------


def _with_feasibility(raw, feasibility):
    raw["feasibility"] = {
        "nutrition_feasible": True,
        "relaxed_constraints": [],
        "dominance_flags": [],
        **feasibility,
    }
    return raw


class TestBudgetStatusMigration:
    def test_legacy_true_cannot_be_trusted(self, foods_by_id):
        """Old code silently skipped unpriced items, so budget_feasible=True
        may hide an understated total: it migrates to UNKNOWN."""
        from models import BudgetStatus

        raw = _with_feasibility(
            _raw_plan(4, foods_by_id, plan_id="p"), {"budget_feasible": True}
        )
        plan = SavedPlan.from_dict(raw, foods_by_id)
        assert plan is not None
        assert plan.budget_status is BudgetStatus.UNKNOWN

    def test_legacy_false_with_nonempty_basket_is_over(self, foods_by_id):
        from models import BudgetStatus

        raw = _with_feasibility(
            _raw_plan(4, foods_by_id, plan_id="p"), {"budget_feasible": False}
        )
        plan = SavedPlan.from_dict(raw, foods_by_id)
        assert plan.budget_status is BudgetStatus.OVER

    def test_legacy_false_with_empty_basket_is_unknown(self, foods_by_id):
        """The old formula was ``bool(items) and total <= budget``: with an
        empty basket, False said nothing about the budget."""
        from models import BudgetStatus

        raw = _with_feasibility(
            _raw_plan(4, foods_by_id, plan_id="p"), {"budget_feasible": False}
        )
        raw["basket"] = []
        plan = SavedPlan.from_dict(raw, foods_by_id)
        assert plan.budget_status is BudgetStatus.UNKNOWN

    def test_unrecognized_status_string_still_loads_as_unknown(self, foods_by_id):
        from models import BudgetStatus

        raw = _with_feasibility(
            _raw_plan(4, foods_by_id, plan_id="p"), {"budget_status": "maybe"}
        )
        plan = SavedPlan.from_dict(raw, foods_by_id)
        assert plan is not None
        assert plan.budget_status is BudgetStatus.UNKNOWN

    def test_all_statuses_round_trip_at_v5(self, foods_by_id):
        from models import BudgetStatus
        from models.plan import PLAN_SCHEMA_VERSION

        for status in BudgetStatus:
            raw = _with_feasibility(
                _raw_plan(4, foods_by_id, plan_id="p"),
                {"budget_status": status.value},
            )
            plan = SavedPlan.from_dict(raw, foods_by_id)
            assert plan.budget_status is status
            dumped = plan.to_dict()
            assert dumped["version"] == PLAN_SCHEMA_VERSION == 5
            assert dumped["feasibility"]["budget_status"] == status.value
            assert "budget_feasible" not in dumped["feasibility"]
            again = SavedPlan.from_dict(dumped, foods_by_id)
            assert again.budget_status is status
