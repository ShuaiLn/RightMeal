"""Pantry flow tests: purchase events (source of truth) and meal consumption."""

from datetime import date

import pytest

from models import Meal, MealPlan, MealPortion, MealSlot, Pantry, SavedBasketItem, SavedPlan
from models.prepared_leftover import (
    STATUS_AVAILABLE,
    STATUS_CONSUMED,
    STATUS_DISCARDED,
    PreparedFoodPortion,
    PreparedLeftover,
    refresh_derived_fields,
)
from models.purchase_log import (
    ORIGIN_LEGACY_MIGRATION,
    PurchaseInput,
    new_purchase_event_id,
)
from services.pantry_flow import (
    can_void_group,
    eat_prepared_leftover,
    latest_group_for_food,
    mark_ingredients_used,
    meal_draw_grams,
    migrate_legacy_purchases,
    purchased_value,
    purchased_grams,
    rebuild_purchase_aggregates,
    record_purchase_event,
    record_purchase_events,
    undo_ingredients_used,
    undo_prepared_leftover,
    void_purchase_group,
)

WHEN = date(2026, 7, 7)


def buy(plan, pantry, log, food_id, grams, *, apply=True, group_id=None, **kwargs):
    """One purchase event with its own group unless one is given."""
    event_id = new_purchase_event_id()
    return record_purchase_event(plan, pantry, log, PurchaseInput(
        event_id=event_id,
        food_id=food_id,
        grams=grams,
        apply_to_plan=apply,
        group_id=group_id or event_id,
        **kwargs,
    ))


def make_plan(basket=()) -> SavedPlan:
    return SavedPlan(
        start_date=WHEN,
        horizon_days=7,
        created_at="2026-07-07T09:00:00",
        budget=50.0,
        total_cost=0.0,
        meal_plan=MealPlan(days=(), horizon_days=7),
        basket=tuple(basket),
        consumed_gaps=(),
    )


def make_basket_item(food, package, count) -> SavedBasketItem:
    return SavedBasketItem(
        food_id=food.id,
        package_id=package.package_id,
        package_label=package.label,
        package_grams=package.grams,
        count=count,
        offer_id=f"test-offer:{package.package_id}",
        cost=1.0,
        source="seed_estimate",
        store="Seed data",
        confidence=1.0,
        match_reason="test",
        matched_product_name=food.name,
    )


def make_meal(portions, slot=MealSlot.DINNER, is_leftover=False, batch_id=None) -> Meal:
    return Meal(
        slot=slot,
        template_id="t",
        name="Test meal",
        portions=tuple(portions),
        is_leftover=is_leftover,
        batch_id=batch_id,
    )


class TestPurchasedGrams:
    def test_purchased_grams_counts_packages(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        pkg = rice.package_options[0]
        items = [make_basket_item(rice, pkg, 2), make_basket_item(rice, pkg, 1)]
        assert purchased_grams(rice, items) == pytest.approx(pkg.grams * 3)

    def test_purchased_grams_ignores_other_foods(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        oil = foods_by_id["canola_oil"]
        items = [make_basket_item(oil, oil.package_options[0], 2)]
        assert purchased_grams(rice, items) == 0.0


class TestPurchaseEvents:
    def test_event_adds_grams_records_baseline_and_aggregate(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        pantry.add(rice.id, 100.0)
        record = buy(plan, pantry, log, rice.id, 900.0)
        assert pantry.items[rice.id] == pytest.approx(1000.0)
        assert plan.purchased[rice.id] == pytest.approx(900.0)  # aggregate cache
        assert record.pantry_grams_before == pytest.approx(100.0)
        assert record.plan_id == plan.plan_id

    def test_off_plan_event_never_touches_the_aggregate(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        record = buy(plan, pantry, log, rice.id, 300.0, apply=False)
        assert record.plan_id is None
        assert pantry.items[rice.id] == pytest.approx(300.0)
        assert plan.purchased == {}

    def test_invalid_inputs_raise(self, foods_by_id):
        plan, pantry, log = make_plan(), Pantry(), []
        with pytest.raises(ValueError):
            buy(plan, pantry, log, "", 100.0)
        with pytest.raises(ValueError):
            buy(plan, pantry, log, "rice_white", 0.0)

    def test_batch_stacks_baselines_for_same_food(self, foods_by_id):
        """Two same-food receipt lines: the second baseline includes the first."""
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        pantry.add(rice.id, 100.0)
        group = new_purchase_event_id()
        inputs = [
            PurchaseInput(event_id=new_purchase_event_id(), food_id=rice.id,
                          grams=500.0, apply_to_plan=True, group_id=group),
            PurchaseInput(event_id=new_purchase_event_id(), food_id=rice.id,
                          grams=300.0, apply_to_plan=True, group_id=group),
        ]
        records = record_purchase_events(plan, pantry, log, inputs)
        assert records[0].pantry_grams_before == pytest.approx(100.0)
        assert records[1].pantry_grams_before == pytest.approx(600.0)
        assert pantry.items[rice.id] == pytest.approx(900.0)
        assert plan.purchased[rice.id] == pytest.approx(800.0)

    def test_explicit_basket_link_is_validated_and_persisted(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        package = rice.package_options[0]
        plan = make_plan([make_basket_item(rice, package, 2)])
        item = plan.basket[0]
        pantry, log = Pantry(), []
        record = buy(
            plan,
            pantry,
            log,
            rice.id,
            package.grams * 2,
            quantity=2,
            basket_item_id=item.basket_item_id,
            package_id=package.package_id,
        )
        assert record.basket_item_id == item.basket_item_id
        assert record.package_id == package.package_id
        assert record.package_label == package.label
        assert record.estimated_line_cost == item.cost

    def test_link_mismatch_fails_before_batch_mutates(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        package = rice.package_options[0]
        plan = make_plan([make_basket_item(rice, package, 1)])
        item = plan.basket[0]
        pantry, log = Pantry(), []
        valid = PurchaseInput(
            event_id="valid",
            food_id=rice.id,
            grams=package.grams,
            quantity=1,
            apply_to_plan=True,
            basket_item_id=item.basket_item_id,
            package_id=package.package_id,
        )
        invalid = PurchaseInput(
            event_id="invalid",
            food_id=rice.id,
            grams=package.grams,
            quantity=1,
            apply_to_plan=True,
            basket_item_id=item.basket_item_id,
            package_id="another-package",
        )
        with pytest.raises(ValueError, match="package does not match"):
            record_purchase_events(plan, pantry, log, [valid, invalid])
        assert pantry.items == {}
        assert log == []
        assert plan.purchased == {}

    def test_generic_purchase_keeps_package_but_never_infers_basket_link(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        package = rice.package_options[0]
        plan = make_plan([make_basket_item(rice, package, 1)])
        pantry, log = Pantry(), []
        record = buy(
            plan,
            pantry,
            log,
            rice.id,
            package.grams,
            package_id=package.package_id,
        )
        assert record.package_id == package.package_id
        assert record.basket_item_id is None


class TestVoidGroup:
    def test_void_restores_exactly_and_marks_never_deletes(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        pantry.add(rice.id, 100.0)
        record = buy(plan, pantry, log, rice.id, 500.0)
        ok, _ = void_purchase_group(plan, pantry, log, record.group_id, today=WHEN)
        assert ok
        assert pantry.items[rice.id] == pytest.approx(100.0)
        assert plan.purchased == {}
        assert len(log) == 1 and log[0].voided_at is not None  # voided, not deleted

    def test_lifo_only_newest_group_can_void(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        first = buy(plan, pantry, log, rice.id, 500.0)
        second = buy(plan, pantry, log, rice.id, 300.0)
        ok, reason = can_void_group(plan, pantry, log, first.group_id, today=WHEN)
        assert not ok and "newer" in reason.lower()
        ok, _ = void_purchase_group(plan, pantry, log, second.group_id, today=WHEN)
        assert ok
        # With the newer event gone, the first becomes voidable.
        ok, _ = void_purchase_group(plan, pantry, log, first.group_id, today=WHEN)
        assert ok
        assert rice.id not in pantry.items
        assert plan.purchased == {}

    def test_consumption_blocks_the_whole_group(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        oil = foods_by_id["canola_oil"]
        plan, pantry, log = make_plan(), Pantry(), []
        group = new_purchase_event_id()
        buy(plan, pantry, log, rice.id, 500.0, group_id=group)
        buy(plan, pantry, log, oil.id, 200.0, group_id=group)
        pantry.remove(rice.id, 100.0)  # cooking used some rice
        ok, reason = can_void_group(plan, pantry, log, group, today=WHEN)
        assert not ok and "used" in reason.lower()
        # All-or-nothing: the safe oil record must not have been voided either.
        assert all(record.voided_at is None for record in log)
        assert pantry.items[oil.id] == pytest.approx(200.0)

    def test_manual_extra_stock_survives_exact_decimal_undo(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        group = new_purchase_event_id()
        buy(plan, pantry, log, rice.id, 0.3, group_id=group)
        pantry.add(rice.id, 0.1)
        ok, reason = void_purchase_group(plan, pantry, log, group, today=WHEN)
        assert ok, reason
        assert pantry.items[rice.id] == 0.1

    def test_newer_off_plan_event_blocks_plan_page_undo(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        plan_buy = buy(plan, pantry, log, rice.id, 500.0)
        buy(plan, pantry, log, rice.id, 300.0, apply=False)  # pantry photo, off-plan
        ok, reason = can_void_group(plan, pantry, log, plan_buy.group_id, today=WHEN)
        assert not ok and "newer" in reason.lower()
        assert latest_group_for_food(log, rice.id) != plan_buy.group_id

    def test_completed_plan_purchases_are_frozen(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        record = buy(plan, pantry, log, rice.id, 500.0)
        after_end = plan.end_date.fromordinal(plan.end_date.toordinal() + 1)
        ok, reason = can_void_group(plan, pantry, log, record.group_id, today=after_end)
        assert not ok and "completed plan" in reason.lower()

    def test_legacy_records_cannot_be_voided(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        plan.purchased[rice.id] = 500.0  # pre-event plan file
        created = migrate_legacy_purchases(plan, log)
        ok, reason = can_void_group(plan, pantry, log, created[0].group_id, today=WHEN)
        assert not ok and "migrated" in reason.lower()


class TestAggregatesAndMigration:
    def test_rebuild_fixes_corrupt_cache_without_touching_pantry(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        buy(plan, pantry, log, rice.id, 500.0)
        plan.purchased[rice.id] = 9999.0  # corrupt the cache
        before = dict(pantry.items)
        rebuild_purchase_aggregates(plan, log)
        assert plan.purchased[rice.id] == pytest.approx(500.0)
        assert pantry.items == before

    def test_voided_records_are_excluded_everywhere(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        record = buy(plan, pantry, log, rice.id, 500.0)
        void_purchase_group(plan, pantry, log, record.group_id, today=WHEN)
        rebuild_purchase_aggregates(plan, log)
        assert plan.purchased == {}
        assert latest_group_for_food(log, rice.id) is None

    def test_migration_is_idempotent_and_never_adds_stock(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        plan.purchased[rice.id] = 500.0
        plan.purchased_baseline[rice.id] = 100.0
        pantry.add(rice.id, 600.0)  # the grams already landed long ago

        first = migrate_legacy_purchases(plan, log)
        assert len(first) == 1
        assert first[0].origin == ORIGIN_LEGACY_MIGRATION
        assert first[0].pantry_grams_before == pytest.approx(100.0)
        assert pantry.items[rice.id] == pytest.approx(600.0)  # untouched

        second = migrate_legacy_purchases(plan, log)  # double-load
        assert second == []
        assert len(log) == 1
        rebuild_purchase_aggregates(plan, log)
        assert plan.purchased[rice.id] == pytest.approx(500.0)
        assert pantry.items[rice.id] == pytest.approx(600.0)
        assert first[0].basket_item_id is None
        assert first[0].package_id is None

    def test_migration_skips_foods_that_already_have_events(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry, log = make_plan(), Pantry(), []
        buy(plan, pantry, log, rice.id, 500.0)
        assert migrate_legacy_purchases(plan, log) == []

    def test_purchased_value_uses_records_not_all_same_food_plan_lines(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        basket = [
            make_basket_item(rice, package, 1)
            for package in rice.package_options[:2]
        ]
        plan, pantry, log = make_plan(basket), Pantry(), []
        buy(
            plan,
            pantry,
            log,
            rice.id,
            100.0,
            estimated_line_cost=1.25,
        )
        buy(plan, pantry, log, rice.id, 50.0)
        assert sum(item.cost for item in plan.basket) == 2.0
        assert purchased_value(plan, log, plan.basket) == 1.25


class TestMealDraws:
    def test_normal_meal_draws_portion_grams(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        oil = foods_by_id["canola_oil"]
        meal = make_meal([MealPortion(rice, 200.0), MealPortion(oil, 15.0)])
        assert meal_draw_grams(meal) == {
            rice.id: pytest.approx(200.0),
            oil.id: pytest.approx(15.0),
        }

    def test_batch_dinner_draws_double(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        meal = make_meal([MealPortion(rice, 200.0)], batch_id="batch-d0-t")
        assert meal_draw_grams(meal) == {rice.id: pytest.approx(400.0)}

    def test_leftover_draws_nothing(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        meal = make_meal(
            [MealPortion(rice, 200.0)],
            slot=MealSlot.LUNCH,
            is_leftover=True,
            batch_id="batch-d0-t",
        )
        assert meal_draw_grams(meal) == {}


class TestMarkIngredientsUsed:
    def test_full_deducts_portion_grams(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry = make_plan(), Pantry(items={rice.id: 1000.0})
        meal = make_meal([MealPortion(rice, 200.0)])
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 1.0)
        assert pantry.items[rice.id] == pytest.approx(800.0)
        entry = plan.tracking_entry(WHEN, meal.slot)
        assert entry["used_fraction"] == 1.0
        assert entry["pantry_deducted"] == {rice.id: pytest.approx(200.0)}

    def test_half_and_custom_fractions(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        for fraction, expected in ((0.5, 100.0), (0.25, 50.0)):
            plan, pantry = make_plan(), Pantry(items={rice.id: 1000.0})
            meal = make_meal([MealPortion(rice, 200.0)])
            mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, fraction)
            assert pantry.items[rice.id] == pytest.approx(1000.0 - expected)
            assert plan.tracking_entry(WHEN, meal.slot)["pantry_deducted"] == {
                rice.id: pytest.approx(expected)
            }

    def test_batch_dinner_deducts_double(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry = make_plan(), Pantry(items={rice.id: 1000.0})
        meal = make_meal([MealPortion(rice, 200.0)], batch_id="batch-d0-t")
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 1.0)
        assert pantry.items[rice.id] == pytest.approx(600.0)

    def test_leftover_deducts_nothing(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry = make_plan(), Pantry(items={rice.id: 1000.0})
        meal = make_meal(
            [MealPortion(rice, 200.0)],
            slot=MealSlot.LUNCH,
            is_leftover=True,
            batch_id="batch-d0-t",
        )
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 1.0)
        assert pantry.items[rice.id] == pytest.approx(1000.0)
        entry = plan.tracking_entry(WHEN, meal.slot)
        assert entry["used_fraction"] == 1.0
        assert entry["pantry_deducted"] == {}

    def test_deduction_clamped_by_available_stock(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry = make_plan(), Pantry(items={rice.id: 120.0})
        meal = make_meal([MealPortion(rice, 200.0)])
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 1.0)
        assert rice.id not in pantry.items
        # only the actually-removed grams are recorded, so undo is exact
        assert plan.tracking_entry(WHEN, meal.slot)["pantry_deducted"] == {
            rice.id: pytest.approx(120.0)
        }

    def test_undo_restores_exactly(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry = make_plan(), Pantry(items={rice.id: 1000.0})
        meal = make_meal([MealPortion(rice, 200.0)])
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 1.0)
        undo_ingredients_used(plan, pantry, WHEN, meal.slot)
        assert pantry.items[rice.id] == pytest.approx(1000.0)
        entry = plan.tracking_entry(WHEN, meal.slot)
        assert entry["used_fraction"] is None
        assert entry["pantry_deducted"] == {}

    def test_undo_after_manual_edit_adds_back_recorded_amount(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry = make_plan(), Pantry(items={rice.id: 1000.0})
        meal = make_meal([MealPortion(rice, 200.0)])
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 1.0)  # -> 800
        pantry.set_grams(rice.id, 100.0)  # user edits stock down manually
        undo_ingredients_used(plan, pantry, WHEN, meal.slot)
        # undo adds back exactly the recorded 200 g on top of the edited stock
        assert pantry.items[rice.id] == pytest.approx(300.0)

    def test_marking_preserves_eaten_and_note(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        plan, pantry = make_plan(), Pantry(items={rice.id: 1000.0})
        meal = make_meal([MealPortion(rice, 200.0)])
        plan.set_tracking(WHEN, meal.slot, eaten=True, leftover_note="froze half")
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 0.5)
        entry = plan.tracking_entry(WHEN, meal.slot)
        assert entry["eaten"] is True
        assert entry["leftover_note"] == "froze half"
        # ...and updating eaten/note preserves the used marker
        plan.set_tracking(WHEN, meal.slot, eaten=False, leftover_note="")
        assert plan.tracking_entry(WHEN, meal.slot)["used_fraction"] == 0.5

    def test_marked_state_is_recorded_once(self, foods_by_id):
        # The UI only offers Undo once marked; the recorded state must reflect
        # the single mark so a double deduction can't happen silently.
        rice = foods_by_id["rice_white"]
        plan, pantry = make_plan(), Pantry(items={rice.id: 1000.0})
        meal = make_meal([MealPortion(rice, 200.0)])
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 1.0)
        assert plan.tracking_entry(WHEN, meal.slot)["used_fraction"] is not None
        undo_ingredients_used(plan, pantry, WHEN, meal.slot)
        mark_ingredients_used(plan, pantry, WHEN, meal.slot, meal, 1.0)
        assert pantry.items[rice.id] == pytest.approx(800.0)


def make_full_leftover(foods_by_id, leftover_id="lo1") -> PreparedLeftover:
    """A fresh (untouched) leftover: one full serving of rice + oil."""
    leftover = PreparedLeftover(
        id=leftover_id,
        origin_kind="user",
        source_date=WHEN.isoformat(),
        source_slot="dinner",
        source_meal_template_id="t",
        meal_name="Test meal",
        note="",
        initial_fraction_remaining=1.0,
        servings_remaining=0.0,
        portions=[
            PreparedFoodPortion("rice_white", "White rice", 200.0, 200.0),
            PreparedFoodPortion("canola_oil", "Canola oil", 20.0, 20.0),
        ],
        prepared_at=WHEN.isoformat(),
        use_by_date="2026-07-10",
        created_at="2026-07-07T20:00:00",
        status=STATUS_AVAILABLE,
    )
    refresh_derived_fields(leftover, foods_by_id)
    return leftover


def make_leftover_meal(foods_by_id, leftover_id="lo1", slot=MealSlot.LUNCH) -> Meal:
    rice = foods_by_id["rice_white"]
    return Meal(
        slot=slot,
        template_id="t",
        name="Leftovers: Test meal",
        portions=(MealPortion(rice, 200.0),),
        is_leftover=True,
        prepared_leftover_id=leftover_id,
    )


class TestPreparedLeftoverDraws:
    def test_scheduled_leftover_meal_draws_nothing(self, foods_by_id):
        meal = make_leftover_meal(foods_by_id)
        assert meal_draw_grams(meal) == {}

    def test_prepared_leftover_id_alone_suppresses_draws(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        meal = Meal(
            slot=MealSlot.LUNCH,
            template_id="t",
            name="x",
            portions=(MealPortion(rice, 200.0),),
            is_leftover=False,  # defensive: the id must be enough on its own
            prepared_leftover_id="lo1",
        )
        assert meal_draw_grams(meal) == {}


class TestEatPreparedLeftover:
    def test_full_consumption(self, foods_by_id):
        plan = make_plan()
        leftover = make_full_leftover(foods_by_id)
        plan.leftovers_used[leftover.id] = 1.0
        reserved, consumed = eat_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        assert (reserved, consumed) == (1.0, pytest.approx(1.0))
        assert leftover.status == STATUS_CONSUMED
        assert all(p.remaining_grams == pytest.approx(0.0) for p in leftover.portions)
        entry = plan.tracking_entry(WHEN, MealSlot.LUNCH)
        assert entry["leftover_consumed"] == pytest.approx(1.0)
        assert entry["leftover_consumed_grams"] == {
            "rice_white": pytest.approx(200.0),
            "canola_oil": pytest.approx(20.0),
        }
        assert entry["leftover_before_grams"] == {
            "rice_white": pytest.approx(200.0),
            "canola_oil": pytest.approx(20.0),
        }

    def test_partial_reservation_consumes_proportionally(self, foods_by_id):
        plan = make_plan()
        leftover = make_full_leftover(foods_by_id)
        plan.leftovers_used[leftover.id] = 0.5
        _, consumed = eat_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        assert consumed == pytest.approx(0.5)
        grams = {p.food_id: p.remaining_grams for p in leftover.portions}
        assert grams == {
            "rice_white": pytest.approx(100.0),
            "canola_oil": pytest.approx(10.0),
        }
        assert leftover.status == STATUS_AVAILABLE
        assert leftover.servings_remaining == pytest.approx(0.5)

    def test_stale_missing_record_marks_zero_consumption(self, foods_by_id):
        plan = make_plan()
        plan.leftovers_used["gone"] = 1.0
        reserved, consumed = eat_prepared_leftover(
            plan, {}, foods_by_id, WHEN, MealSlot.LUNCH, "gone"
        )
        assert (reserved, consumed) == (1.0, 0.0)
        entry = plan.tracking_entry(WHEN, MealSlot.LUNCH)
        assert entry["leftover_consumed"] == 0.0  # recorded: can't double-consume later

    def test_stale_discarded_record_consumes_nothing(self, foods_by_id):
        plan = make_plan()
        leftover = make_full_leftover(foods_by_id)
        leftover.status = STATUS_DISCARDED
        plan.leftovers_used[leftover.id] = 1.0
        _, consumed = eat_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        assert consumed == 0.0
        assert all(p.remaining_grams == pytest.approx(200.0) or
                   p.remaining_grams == pytest.approx(20.0) for p in leftover.portions)

    def test_insufficient_servings_consume_what_is_left(self, foods_by_id):
        plan = make_plan()
        leftover = make_full_leftover(foods_by_id)
        for p in leftover.portions:
            p.remaining_grams *= 0.3  # someone ate most of it from the Pantry page
        refresh_derived_fields(leftover, foods_by_id)
        plan.leftovers_used[leftover.id] = 1.0
        reserved, consumed = eat_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        assert reserved == 1.0
        assert consumed == pytest.approx(0.3)
        assert leftover.status == STATUS_CONSUMED


class TestUndoPreparedLeftover:
    def test_undo_restores_exactly_and_revives_status(self, foods_by_id):
        plan = make_plan()
        leftover = make_full_leftover(foods_by_id)
        plan.leftovers_used[leftover.id] = 1.0
        eat_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        undo_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        assert leftover.status == STATUS_AVAILABLE
        assert {p.food_id: p.remaining_grams for p in leftover.portions} == {
            "rice_white": pytest.approx(200.0),
            "canola_oil": pytest.approx(20.0),
        }
        assert leftover.servings_remaining == pytest.approx(1.0)
        entry = plan.tracking_entry(WHEN, MealSlot.LUNCH)
        assert entry["leftover_consumed"] is None
        assert entry["leftover_consumed_grams"] is None
        assert entry["leftover_before_grams"] is None

    def test_undo_after_adjust_is_capped_by_before_snapshot(self, foods_by_id):
        plan = make_plan()
        leftover = make_full_leftover(foods_by_id)
        plan.leftovers_used[leftover.id] = 0.5
        eat_prepared_leftover(  # rice 200 -> 100, oil 20 -> 10
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        # User then adjusts the remaining rice down to 40 g from the Pantry page.
        leftover.portions[0].remaining_grams = 40.0
        refresh_derived_fields(leftover, foods_by_id)
        undo_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        # 40 + 100 consumed = 140, and the cap (before-snapshot 200) is not hit;
        # the oil hits its cap exactly: 10 + 10 = 20.
        assert leftover.portions[0].remaining_grams == pytest.approx(140.0)
        assert leftover.portions[1].remaining_grams == pytest.approx(20.0)

    def test_undo_never_exceeds_before_snapshot(self, foods_by_id):
        plan = make_plan()
        leftover = make_full_leftover(foods_by_id)
        for p in leftover.portions:
            p.remaining_grams *= 0.5  # start half-eaten: before-snapshot = 100/10
        refresh_derived_fields(leftover, foods_by_id)
        plan.leftovers_used[leftover.id] = 0.5
        eat_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        # Someone manually bumps a portion up before the undo.
        leftover.portions[0].remaining_grams = 90.0
        undo_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        assert leftover.portions[0].remaining_grams == pytest.approx(100.0)  # capped

    def test_undo_preserves_eaten_and_note(self, foods_by_id):
        plan = make_plan()
        leftover = make_full_leftover(foods_by_id)
        plan.leftovers_used[leftover.id] = 1.0
        plan.set_tracking(WHEN, MealSlot.LUNCH, eaten=True, leftover_note="good")
        eat_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        undo_prepared_leftover(
            plan, {leftover.id: leftover}, foods_by_id, WHEN, MealSlot.LUNCH, leftover.id
        )
        entry = plan.tracking_entry(WHEN, MealSlot.LUNCH)
        assert entry["eaten"] is True
        assert entry["leftover_note"] == "good"

    def test_undo_with_missing_record_only_clears_tracking(self, foods_by_id):
        plan = make_plan()
        plan.set_leftover_consumption(WHEN, MealSlot.LUNCH, 0.5, {"rice_white": 100.0},
                                      {"rice_white": 200.0})
        undo_prepared_leftover(plan, {}, foods_by_id, WHEN, MealSlot.LUNCH, "gone")
        assert plan.tracking_entry(WHEN, MealSlot.LUNCH)["leftover_consumed"] is None
