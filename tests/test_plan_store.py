"""Saved-plan persistence tests using a temporary directory."""

import json
from datetime import date

import pytest

from models import (
    Explanation,
    HouseholdProfile,
    MealSlot,
    SavedBasketItem,
    SavedPlan,
    SavedUnusedFood,
)
from models.plan import PLAN_SCHEMA_VERSION, _default_tracking_entry
from planner import consumed_gaps
from services.plan_store import PlanStore

from conftest import make_recipe_meal_plan, result_from_demand


def entry_with(**overrides) -> dict:
    """Expected tracking entry: the defaults plus the given fields."""
    entry = _default_tracking_entry()
    entry.update(overrides)
    return entry


@pytest.fixture(scope="module")
def saved_plan(foods, foods_by_id, seed_quotes, nutrition):
    profile = HouseholdProfile(adults=2, children=2, city="Los Angeles", zip_code="90001")
    demand = {"rice_white": 2000.0, "chicken_breast": 1400.0, "eggs_large": 900.0,
              "milk_whole": 3000.0, "carrots": 900.0, "broccoli_frozen": 800.0,
              "bread_whole_wheat": 1200.0, "rolled_oats": 700.0, "bananas": 1200.0}
    result = result_from_demand(demand, profile, 60.0, 7, foods_by_id, seed_quotes, nutrition)
    plan = make_recipe_meal_plan(foods_by_id, horizon=7)
    targets = nutrition.household_targets(profile, 7)
    return SavedPlan(
        start_date=date(2026, 7, 7),
        horizon_days=7,
        created_at="2026-07-07T09:00:00",
        budget=60.0,
        total_cost=result.total_cost,
        meal_plan=plan,
        basket=tuple(
            SavedBasketItem(
                food_id=item.food.id,
                package_label=item.package.label,
                count=item.count,
                cost=round(item.cost, 2),
                source=item.quote.source.value,
                store=item.quote.store,
                confidence=item.quote.confidence,
                match_reason=item.quote.match_reason,
                matched_product_name=item.quote.matched_product_name,
            )
            for item in result.items
        ),
        consumed_gaps=tuple(consumed_gaps(plan, targets)),
        tracking={},
        purchased_totals=result.nutrient_totals,
        explanation=Explanation(summary="A budget-balanced basket.", generated_by="local"),
        nutrition_feasible=result.nutrition_feasible,
        budget_status=result.budget_status,
        relaxed_constraints=result.relaxed_constraints,
        dominance_flags=result.dominance_flags,
        unused=(
            SavedUnusedFood(
                category="not_selected", food_id=foods[0].id, reason="not chosen this time"
            ),
        ),
    )


def test_first_run_returns_none(tmp_path, foods_by_id):
    assert PlanStore(tmp_path).load(foods_by_id) is None


def test_round_trip(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    loaded = store.load(foods_by_id)
    assert loaded is not None
    assert loaded.start_date == saved_plan.start_date
    assert loaded.horizon_days == 7
    assert loaded.total_cost == saved_plan.total_cost
    assert [item.food_id for item in loaded.basket] == [item.food_id for item in saved_plan.basket]
    original = [
        (m.slot.value, m.template_id, m.name, m.is_leftover, m.batch_id)
        for d in saved_plan.meal_plan.days
        for m in d.meals
    ]
    reloaded = [
        (m.slot.value, m.template_id, m.name, m.is_leftover, m.batch_id)
        for d in loaded.meal_plan.days
        for m in d.meals
    ]
    assert reloaded == original
    # nutrients recomputed on load land within rounding tolerance (grams 3 dp)
    assert loaded.meal_plan.consumed_totals.calories_kcal == pytest.approx(
        saved_plan.meal_plan.consumed_totals.calories_kcal, rel=1e-4
    )
    assert set(loaded.meal_plan.pantry_carryover) == set(saved_plan.meal_plan.pantry_carryover)
    # v2 fields survive the round trip
    assert loaded.purchased_totals.calories_kcal == pytest.approx(
        saved_plan.purchased_totals.calories_kcal, rel=1e-3
    )
    assert loaded.explanation is not None
    assert loaded.explanation.summary == saved_plan.explanation.summary
    assert loaded.explanation.generated_by == "local"
    assert loaded.nutrition_feasible == saved_plan.nutrition_feasible
    assert loaded.budget_status == saved_plan.budget_status
    assert loaded.relaxed_constraints == saved_plan.relaxed_constraints
    assert loaded.unused == saved_plan.unused


def test_corrupt_file_returns_none(tmp_path, foods_by_id):
    store = PlanStore(tmp_path)
    store.base_dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load(foods_by_id) is None


def test_unknown_food_id_returns_none(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["meal_plan"]["days"][0]["meals"][0]["portions"][0]["food_id"] = "no_such_food"
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load(foods_by_id) is None


def test_version_bump_returns_none(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["version"] = 999
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load(foods_by_id) is None


def test_tracking_save_and_reload(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    plan = store.load(foods_by_id)
    when = plan.start_date
    plan.set_tracking(when, MealSlot.BREAKFAST, eaten=True, leftover_note="froze half")
    store.save(plan)
    again = store.load(foods_by_id)
    entry = again.tracking_entry(when, MealSlot.BREAKFAST)
    assert entry == entry_with(eaten=True, leftover_note="froze half")
    # untouched slots default to not eaten
    assert again.tracking_entry(when, MealSlot.DINNER) == entry_with()


def test_ingredients_used_tracking_round_trip(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    plan = store.load(foods_by_id)
    when = plan.start_date
    food_id = plan.basket[0].food_id
    plan.set_tracking(when, MealSlot.LUNCH, eaten=True, leftover_note="tasty")
    plan.set_ingredients_used(when, MealSlot.LUNCH, 0.5, {food_id: 123.0})
    store.save(plan)
    again = store.load(foods_by_id)
    entry = again.tracking_entry(when, MealSlot.LUNCH)
    assert entry["eaten"] is True
    assert entry["leftover_note"] == "tasty"
    assert entry["used_fraction"] == 0.5
    assert entry["pantry_deducted"] == {food_id: 123.0}
    again.clear_ingredients_used(when, MealSlot.LUNCH)
    cleared = again.tracking_entry(when, MealSlot.LUNCH)
    assert cleared["used_fraction"] is None
    assert cleared["pantry_deducted"] == {}
    assert cleared["eaten"] is True  # eaten/note survive the clear


def test_legacy_tracking_entries_still_load(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    when = saved_plan.start_date.isoformat()
    # a pre-pantry file: entries carry only the two original keys
    data["tracking"] = {when: {"breakfast": {"eaten": True, "leftover_note": "old"}}}
    data.pop("purchased", None)
    data.pop("pantry_used", None)
    store.path.write_text(json.dumps(data), encoding="utf-8")
    plan = store.load(foods_by_id)
    assert plan is not None
    entry = plan.tracking_entry(saved_plan.start_date, MealSlot.BREAKFAST)
    assert entry == entry_with(eaten=True, leftover_note="old")
    assert plan.purchased == {}
    assert plan.pantry_used == {}
    assert plan.purchased_baseline == {}
    assert plan.leftovers_used == {}


def test_purchased_and_pantry_used_round_trip(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    plan = store.load(foods_by_id)
    food_id = plan.basket[0].food_id
    plan.purchased[food_id] = 907.0
    plan.pantry_used = {food_id: 250.0}
    store.save(plan)
    again = store.load(foods_by_id)
    assert again.purchased == {food_id: 907.0}
    assert again.pantry_used == {food_id: 250.0}


def test_unknown_purchased_ids_dropped_on_load(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["purchased"] = {"no_such_food": 100.0}
    data["pantry_used"] = {"no_such_food": 100.0}
    store.path.write_text(json.dumps(data), encoding="utf-8")
    plan = store.load(foods_by_id)
    assert plan is not None
    assert plan.purchased == {}
    assert plan.pantry_used == {}


def test_delete_idempotent(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    assert store.load(foods_by_id) is not None
    store.delete()
    assert store.load(foods_by_id) is None
    store.delete()  # no error on second delete


def test_leftover_fields_round_trip(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    plan = store.load(foods_by_id)
    when = plan.start_date
    food_id = plan.basket[0].food_id
    plan.leftovers_used["lo-abc"] = 0.75
    plan.purchased_baseline[food_id] = 120.0
    plan.set_prepared(when, MealSlot.DINNER, True)
    plan.set_leftover_consumption(
        when, MealSlot.DINNER, 0.5, {food_id: 60.0}, {food_id: 130.0}
    )
    plan.set_leftover_link(when, MealSlot.DINNER, "leftover_created_id", "lo-abc")
    plan.set_leftover_link(when, MealSlot.DINNER, "batch_leftover_id", "lo-batch")
    plan.set_leftover_link(when, MealSlot.LUNCH, "linked_leftover_id", "lo-batch")
    store.save(plan)
    again = store.load(foods_by_id)
    assert again.leftovers_used == {"lo-abc": 0.75}
    assert again.purchased_baseline == {food_id: 120.0}
    dinner = again.tracking_entry(when, MealSlot.DINNER)
    assert dinner["prepared"] is True
    assert dinner["leftover_consumed"] == 0.5
    assert dinner["leftover_consumed_grams"] == {food_id: 60.0}
    assert dinner["leftover_before_grams"] == {food_id: 130.0}
    assert dinner["leftover_created_id"] == "lo-abc"
    assert dinner["batch_leftover_id"] == "lo-batch"
    assert again.tracking_entry(when, MealSlot.LUNCH)["linked_leftover_id"] == "lo-batch"


def test_prepared_leftover_meal_id_round_trip(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["meal_plan"]["days"][0]["meals"][0]["prepared_leftover_id"] = "lo-xyz"
    store.path.write_text(json.dumps(data), encoding="utf-8")
    plan = store.load(foods_by_id)
    assert plan.meal_plan.days[0].meals[0].prepared_leftover_id == "lo-xyz"
    other = plan.meal_plan.days[0].meals[1]
    assert other.prepared_leftover_id is None


def test_files_without_new_keys_still_load(tmp_path, foods_by_id, saved_plan):
    """A plan saved before the leftover feature (no new keys anywhere) loads
    with defaults."""
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["version"] == PLAN_SCHEMA_VERSION == 5
    data.pop("leftovers_used", None)
    data.pop("purchased_baseline", None)
    for day in data["meal_plan"]["days"]:
        for meal in day["meals"]:
            meal.pop("prepared_leftover_id", None)
    when = saved_plan.start_date.isoformat()
    data["tracking"] = {
        when: {"dinner": {"eaten": True, "leftover_note": "", "used_fraction": 1.0,
                          "pantry_deducted": {}}}
    }
    store.path.write_text(json.dumps(data), encoding="utf-8")
    plan = store.load(foods_by_id)
    assert plan is not None
    assert plan.leftovers_used == {}
    assert plan.purchased_baseline == {}
    assert all(
        m.prepared_leftover_id is None for d in plan.meal_plan.days for m in d.meals
    )
    entry = plan.tracking_entry(saved_plan.start_date, MealSlot.DINNER)
    assert entry == entry_with(eaten=True, used_fraction=1.0)


def test_v3_round_trip_keeps_plan_id_without_resave_flag(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    loaded = store.load(foods_by_id)
    assert loaded.plan_id == saved_plan.plan_id
    assert loaded.needs_resave is False


def test_v2_plan_migrates_with_deterministic_plan_id(tmp_path, foods_by_id, saved_plan):
    """A v2 file (no plan_id) loads, derives the SAME plan_id every time —
    even across interrupted saves — and asks for one resave."""
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["version"] = 2
    data.pop("plan_id", None)
    store.path.write_text(json.dumps(data), encoding="utf-8")

    first = store.load(foods_by_id)
    second = store.load(foods_by_id)
    assert first is not None and second is not None
    assert first.plan_id == second.plan_id  # deterministic across retries
    assert first.plan_id != saved_plan.plan_id  # derived, not invented per-load
    assert first.needs_resave is True

    store.save(first)  # the startup resave
    resaved = store.load(foods_by_id)
    assert resaved.plan_id == first.plan_id
    assert resaved.needs_resave is False


def test_unknown_version_returns_none(tmp_path, foods_by_id, saved_plan):
    store = PlanStore(tmp_path)
    store.save(saved_plan)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["version"] = 1
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load(foods_by_id) is None
