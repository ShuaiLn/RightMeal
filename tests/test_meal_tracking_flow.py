"""Domain-layer meal tracking: prepare/eat, leftovers, undo gating, rollback."""

from datetime import date, timedelta

import pytest

from models import DayPlan, Meal, MealPlan, MealPortion, MealSlot, Pantry, SavedPlan
from models.prepared_leftover import (
    STATUS_AVAILABLE,
    STATUS_CONSUMED,
    STATUS_DISCARDED,
    PreparedFoodPortion,
    PreparedLeftover,
    refresh_derived_fields,
)
from services.meal_tracking_flow import (
    can_edit_leftover,
    can_undo_preparation,
    correct_display_status,
    correct_leftover_note,
    meal_was_prepared,
    prepare_and_eat,
    record_leftover,
    reserved_slot_for,
    undo_preparation,
)

WHEN = date(2026, 7, 7)
NEXT = WHEN + timedelta(days=1)


class StubState:
    """The TrackingState slice, with a switchable failing persist."""

    def __init__(self, foods_by_id, pantry=None, leftovers=None):
        self.pantry = pantry if pantry is not None else Pantry()
        self.prepared_leftovers = list(leftovers or [])
        self._foods_by_id = foods_by_id
        self.fail_persist = False
        self.persist_calls = 0

    @property
    def foods_by_id(self):
        return self._foods_by_id

    @property
    def leftovers_by_id(self):
        return {lo.id: lo for lo in self.prepared_leftovers}

    def persist(self, *, plan=None, pantry=None, leftovers=None):
        self.persist_calls += 1
        if self.fail_persist:
            raise OSError("disk full")


def make_meal(portions, slot=MealSlot.DINNER, is_leftover=False, batch_id=None,
              prepared_leftover_id=None, template_id="t", name="Test meal") -> Meal:
    return Meal(
        slot=slot,
        template_id=template_id,
        name=name,
        portions=tuple(portions),
        is_leftover=is_leftover,
        batch_id=batch_id,
        prepared_leftover_id=prepared_leftover_id,
    )


def make_plan(days=()) -> SavedPlan:
    return SavedPlan(
        start_date=WHEN,
        horizon_days=max(len(days), 7),
        created_at="2026-07-07T09:00:00",
        budget=50.0,
        total_cost=0.0,
        meal_plan=MealPlan(days=tuple(days), horizon_days=max(len(days), 7)),
        basket=(),
        consumed_gaps=(),
    )


def rice_chicken_meal(foods_by_id, **kwargs) -> Meal:
    rice = foods_by_id["rice_white"]
    chicken = foods_by_id["chicken_breast"]
    return make_meal([MealPortion(rice, 200.0), MealPortion(chicken, 300.0)], **kwargs)


def make_ready_leftover(foods_by_id, leftover_id="lo1") -> PreparedLeftover:
    leftover = PreparedLeftover(
        id=leftover_id,
        origin_kind="user",
        source_date=WHEN.isoformat(),
        source_slot="dinner",
        source_meal_template_id="t",
        meal_name="Old stew",
        note="",
        initial_fraction_remaining=1.0,
        servings_remaining=0.0,
        portions=[PreparedFoodPortion("rice_white", "White rice", 200.0, 200.0)],
        prepared_at=WHEN.isoformat(),
        use_by_date="2026-07-10",
        created_at="2026-07-07T20:00:00",
        status=STATUS_AVAILABLE,
    )
    refresh_derived_fields(leftover, foods_by_id)
    return leftover


class TestPrepareAndEat:
    def test_normal_meal_deducts_and_marks(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        result = prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        assert result.ok and result.message is None
        assert state.pantry.items["rice_white"] == pytest.approx(800.0)
        assert state.pantry.items["chicken_breast"] == pytest.approx(200.0)
        entry = plan.tracking_entry(WHEN, meal.slot)
        assert entry["eaten"] is True
        assert entry["prepared"] is True
        assert entry["used_fraction"] == 1.0

    def test_double_click_deducts_once(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        assert state.pantry.items["rice_white"] == pytest.approx(800.0)

    def test_correct_display_then_eaten_never_deducts_twice(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        correct_display_status(state, plan, WHEN, meal.slot)
        entry = plan.tracking_entry(WHEN, meal.slot)
        assert entry["eaten"] is False
        assert meal_was_prepared(entry)  # prepared survives the display reset
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        assert state.pantry.items["rice_white"] == pytest.approx(800.0)  # once
        assert plan.tracking_entry(WHEN, meal.slot)["eaten"] is True

    def test_leftover_backed_meal_consumes_record_not_pantry(self, foods_by_id):
        leftover = make_ready_leftover(foods_by_id)
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}), [leftover])
        plan = make_plan()
        plan.leftovers_used[leftover.id] = 1.0
        meal = make_meal([MealPortion(foods_by_id["rice_white"], 200.0)],
                         slot=MealSlot.LUNCH, is_leftover=True,
                         prepared_leftover_id=leftover.id)
        result = prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        assert result.ok and result.message is None
        assert state.pantry.items["rice_white"] == pytest.approx(1000.0)  # untouched
        assert leftover.status == STATUS_CONSUMED

    def test_leftover_backed_double_click_consumes_once(self, foods_by_id):
        leftover = make_ready_leftover(foods_by_id)
        state = StubState(foods_by_id, leftovers=[leftover])
        plan = make_plan()
        plan.leftovers_used[leftover.id] = 0.5
        meal = make_meal([MealPortion(foods_by_id["rice_white"], 200.0)],
                         slot=MealSlot.LUNCH, is_leftover=True,
                         prepared_leftover_id=leftover.id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        correct_display_status(state, plan, WHEN, meal.slot)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        assert leftover.servings_remaining == pytest.approx(0.5)  # consumed once

    def test_stale_leftover_still_marks_eaten_with_message(self, foods_by_id):
        state = StubState(foods_by_id)  # record vanished entirely
        plan = make_plan()
        plan.leftovers_used["gone"] = 1.0
        meal = make_meal([MealPortion(foods_by_id["rice_white"], 200.0)],
                         slot=MealSlot.LUNCH, is_leftover=True,
                         prepared_leftover_id="gone")
        result = prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        assert result.ok
        assert result.message is not None  # "already used up" warning
        assert plan.tracking_entry(WHEN, meal.slot)["eaten"] is True

    def test_persist_failure_rolls_back_memory(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        state.fail_persist = True
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        result = prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        assert not result.ok
        assert state.pantry.items["rice_white"] == pytest.approx(1000.0)
        assert plan.tracking_entry(WHEN, meal.slot)["eaten"] is False
        assert not meal_was_prepared(plan.tracking_entry(WHEN, meal.slot))


def batch_plan(foods_by_id, lunch_batch_id="b1", with_lunch=True):
    """Day 0: batch dinner; day 1: its leftover lunch (identity configurable)."""
    rice = foods_by_id["rice_white"]
    dinner = make_meal([MealPortion(rice, 200.0)], slot=MealSlot.DINNER, batch_id="b1")
    days = [DayPlan(day_index=0, meals=(dinner,))]
    if with_lunch:
        lunch = make_meal([MealPortion(rice, 200.0)], slot=MealSlot.LUNCH,
                          is_leftover=True, batch_id=lunch_batch_id)
        days.append(DayPlan(day_index=1, meals=(lunch,)))
    else:
        days.append(DayPlan(day_index=1, meals=()))
    return make_plan(days), dinner


class TestBatchLeftover:
    def test_batch_dinner_creates_reserved_record(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}))
        plan, dinner = batch_plan(foods_by_id)
        result = prepare_and_eat(state, plan, WHEN, dinner.slot, dinner)
        assert result.ok
        assert state.pantry.items["rice_white"] == pytest.approx(600.0)  # 2x draw
        assert len(state.prepared_leftovers) == 1
        record = state.prepared_leftovers[0]
        assert record.origin_kind == "batch"
        assert record.servings_remaining == pytest.approx(1.0)
        assert {p.food_id: p.original_grams for p in record.portions} == {
            "rice_white": pytest.approx(200.0)  # ONE serving, not the 2x draw
        }
        assert plan.leftovers_used == {record.id: 1.0}
        assert plan.tracking_entry(WHEN, dinner.slot)["batch_leftover_id"] == record.id
        assert plan.tracking_entry(NEXT, MealSlot.LUNCH)["linked_leftover_id"] == record.id
        assert reserved_slot_for(plan, record.id) == (NEXT, MealSlot.LUNCH)

    def test_linked_lunch_eaten_consumes_the_record(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}))
        plan, dinner = batch_plan(foods_by_id)
        prepare_and_eat(state, plan, WHEN, dinner.slot, dinner)
        record = state.prepared_leftovers[0]
        lunch = plan.meal_plan.days[1].meal_for(MealSlot.LUNCH)
        result = prepare_and_eat(state, plan, NEXT, MealSlot.LUNCH, lunch)
        assert result.ok and result.message is None
        assert record.status == STATUS_CONSUMED
        assert state.pantry.items["rice_white"] == pytest.approx(600.0)  # no extra draw

    def test_lunch_eaten_first_means_no_ghost_record(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}))
        plan, dinner = batch_plan(foods_by_id)
        lunch = plan.meal_plan.days[1].meal_for(MealSlot.LUNCH)
        prepare_and_eat(state, plan, NEXT, MealSlot.LUNCH, lunch)  # out of order
        prepare_and_eat(state, plan, WHEN, dinner.slot, dinner)
        assert state.prepared_leftovers == []  # the second serving was eaten

    def test_mismatched_batch_id_stores_unreserved(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}))
        plan, dinner = batch_plan(foods_by_id, lunch_batch_id="other-batch")
        prepare_and_eat(state, plan, WHEN, dinner.slot, dinner)
        record = state.prepared_leftovers[0]
        assert plan.leftovers_used == {}  # not reserved
        assert plan.tracking_entry(NEXT, MealSlot.LUNCH)["linked_leftover_id"] is None
        assert reserved_slot_for(plan, record.id) is None

    def test_no_next_day_lunch_stores_unreserved(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}))
        plan, dinner = batch_plan(foods_by_id, with_lunch=False)
        prepare_and_eat(state, plan, WHEN, dinner.slot, dinner)
        assert len(state.prepared_leftovers) == 1
        assert plan.leftovers_used == {}


class TestRecordLeftover:
    def test_components_override_overall_and_pantry_untouched(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        pantry_after_eating = dict(state.pantry.items)
        result = record_leftover(
            state, plan, WHEN, meal.slot, meal,
            overall_fraction=0.33,
            components={"rice_white": 0.5, "chicken_breast": 0.0},
            note="half the rice is left and all the chicken was eaten",
        )
        assert result.ok
        assert state.pantry.items == pantry_after_eating  # never refunded
        record = state.prepared_leftovers[0]
        grams = {p.food_id: p.remaining_grams for p in record.portions}
        assert grams == {"rice_white": pytest.approx(100.0),
                         "chicken_breast": pytest.approx(0.0)}
        entry = plan.tracking_entry(WHEN, meal.slot)
        assert entry["leftover_created_id"] == record.id
        assert entry["leftover_note"] == (
            "half the rice is left and all the chicken was eaten"
        )

    def test_servings_are_derived_not_taken_from_overall(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        # Contradictory AI: overall says 10% but components say everything left.
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.1,
                        {"rice_white": 1.0, "chicken_breast": 1.0}, "huh")
        record = state.prepared_leftovers[0]
        assert record.servings_remaining == pytest.approx(1.0)  # from components
        assert record.initial_fraction_remaining == pytest.approx(1.0)  # display too

    def test_same_food_portions_aggregate(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}))
        plan = make_plan()
        meal = make_meal([MealPortion(rice, 120.0), MealPortion(rice, 80.0)])
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.5, {}, "half")
        record = state.prepared_leftovers[0]
        assert len(record.portions) == 1
        assert record.portions[0].original_grams == pytest.approx(200.0)
        assert record.portions[0].remaining_grams == pytest.approx(100.0)

    def test_editing_updates_in_place_without_duplicates(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.4, {}, "40%")
        first_id = state.prepared_leftovers[0].id
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.2, {}, "actually 20%")
        assert len(state.prepared_leftovers) == 1
        record = state.prepared_leftovers[0]
        assert record.id == first_id
        assert record.servings_remaining == pytest.approx(0.2)
        assert record.note == "actually 20%"

    def test_zero_fraction_creates_nothing_and_deletes_existing(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.0, {}, "all gone")
        assert state.prepared_leftovers == []
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.4, {}, "40%")
        assert len(state.prepared_leftovers) == 1
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.0, {}, "mark fully eaten")
        assert state.prepared_leftovers == []
        assert plan.tracking_entry(WHEN, meal.slot)["leftover_created_id"] is None

    def test_requires_eaten_first_and_rejects_leftover_meals(self, foods_by_id):
        state = StubState(foods_by_id)
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        assert not record_leftover(state, plan, WHEN, meal.slot, meal, 0.5, {}, "x").ok
        ready = make_meal([MealPortion(foods_by_id["rice_white"], 100.0)],
                          is_leftover=True, prepared_leftover_id="lo9")
        assert not record_leftover(state, plan, WHEN, ready.slot, ready, 0.5, {}, "x").ok

    def test_downstream_history_blocks_editing(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.5, {}, "half")
        record = state.prepared_leftovers[0]
        # Someone eats part of it from the Pantry page.
        for p in record.portions:
            p.remaining_grams *= 0.5
        refresh_derived_fields(record, foods_by_id)
        assert not can_edit_leftover(state, plan, WHEN, meal.slot)
        assert not record_leftover(state, plan, WHEN, meal.slot, meal, 0.3, {}, "no").ok
        # ...but the note-only correction still works and touches no inventory.
        servings_before = record.servings_remaining
        assert correct_leftover_note(state, plan, WHEN, meal.slot, "fixed note").ok
        assert record.note == "fixed note"
        assert record.servings_remaining == pytest.approx(servings_before)

    def test_reserved_record_blocks_editing(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        record_leftover(state, plan, WHEN, meal.slot, meal, 0.5, {}, "half")
        plan.leftovers_used[state.prepared_leftovers[0].id] = 0.5  # plan reserved it
        assert not can_edit_leftover(state, plan, WHEN, meal.slot)

    def test_persist_failure_rolls_back(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        state.fail_persist = True
        result = record_leftover(state, plan, WHEN, meal.slot, meal, 0.5, {}, "half")
        assert not result.ok
        assert state.prepared_leftovers == []
        assert plan.tracking_entry(WHEN, meal.slot)["leftover_created_id"] is None


class TestUndoPreparation:
    def prepared_with_leftover(self, foods_by_id, fraction=0.5):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0,
                                                     "chicken_breast": 500.0}))
        plan = make_plan()
        meal = rice_chicken_meal(foods_by_id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        if fraction:
            record_leftover(state, plan, WHEN, meal.slot, meal, fraction, {}, "note")
        return state, plan, meal

    def test_clean_undo_restores_everything(self, foods_by_id):
        state, plan, meal = self.prepared_with_leftover(foods_by_id)
        assert can_undo_preparation(state, plan, WHEN, meal.slot, meal)
        result = undo_preparation(state, plan, WHEN, meal.slot, meal)
        assert result.ok
        assert state.pantry.items["rice_white"] == pytest.approx(1000.0)
        assert state.pantry.items["chicken_breast"] == pytest.approx(500.0)
        assert state.prepared_leftovers == []
        entry = plan.tracking_entry(WHEN, meal.slot)
        assert entry["eaten"] is False
        assert not meal_was_prepared(entry)
        assert entry["leftover_created_id"] is None

    def test_partially_eaten_leftover_blocks_undo(self, foods_by_id):
        state, plan, meal = self.prepared_with_leftover(foods_by_id)
        record = state.prepared_leftovers[0]
        for p in record.portions:
            p.remaining_grams *= 0.6
        refresh_derived_fields(record, foods_by_id)
        assert not can_undo_preparation(state, plan, WHEN, meal.slot, meal)
        assert not undo_preparation(state, plan, WHEN, meal.slot, meal).ok

    def test_discarded_leftover_blocks_undo(self, foods_by_id):
        state, plan, meal = self.prepared_with_leftover(foods_by_id)
        state.prepared_leftovers[0].status = STATUS_DISCARDED
        assert not can_undo_preparation(state, plan, WHEN, meal.slot, meal)

    def test_reserved_leftover_blocks_undo(self, foods_by_id):
        state, plan, meal = self.prepared_with_leftover(foods_by_id)
        plan.leftovers_used[state.prepared_leftovers[0].id] = 0.5
        assert not can_undo_preparation(state, plan, WHEN, meal.slot, meal)

    def test_batch_undo_clears_record_reservation_and_links(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}))
        plan, dinner = batch_plan(foods_by_id)
        prepare_and_eat(state, plan, WHEN, dinner.slot, dinner)
        assert can_undo_preparation(state, plan, WHEN, dinner.slot, dinner)
        result = undo_preparation(state, plan, WHEN, dinner.slot, dinner)
        assert result.ok
        assert state.pantry.items["rice_white"] == pytest.approx(1000.0)
        assert state.prepared_leftovers == []
        assert plan.leftovers_used == {}
        assert plan.tracking_entry(WHEN, dinner.slot)["batch_leftover_id"] is None
        assert plan.tracking_entry(NEXT, MealSlot.LUNCH)["linked_leftover_id"] is None

    def test_batch_undo_blocked_after_lunch_eaten(self, foods_by_id):
        state = StubState(foods_by_id, Pantry(items={"rice_white": 1000.0}))
        plan, dinner = batch_plan(foods_by_id)
        prepare_and_eat(state, plan, WHEN, dinner.slot, dinner)
        lunch = plan.meal_plan.days[1].meal_for(MealSlot.LUNCH)
        prepare_and_eat(state, plan, NEXT, MealSlot.LUNCH, lunch)
        assert not can_undo_preparation(state, plan, WHEN, dinner.slot, dinner)

    def test_leftover_backed_meal_undo_refills_record(self, foods_by_id):
        leftover = make_ready_leftover(foods_by_id)
        state = StubState(foods_by_id, leftovers=[leftover])
        plan = make_plan()
        plan.leftovers_used[leftover.id] = 1.0
        meal = make_meal([MealPortion(foods_by_id["rice_white"], 200.0)],
                         slot=MealSlot.LUNCH, is_leftover=True,
                         prepared_leftover_id=leftover.id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        assert can_undo_preparation(state, plan, WHEN, meal.slot, meal)
        undo_preparation(state, plan, WHEN, meal.slot, meal)
        assert leftover.status == STATUS_AVAILABLE
        assert leftover.servings_remaining == pytest.approx(1.0)
        assert plan.tracking_entry(WHEN, meal.slot)["eaten"] is False

    def test_leftover_backed_undo_blocked_after_record_touched(self, foods_by_id):
        leftover = make_ready_leftover(foods_by_id)
        state = StubState(foods_by_id, leftovers=[leftover])
        plan = make_plan()
        plan.leftovers_used[leftover.id] = 0.5
        meal = make_meal([MealPortion(foods_by_id["rice_white"], 200.0)],
                         slot=MealSlot.LUNCH, is_leftover=True,
                         prepared_leftover_id=leftover.id)
        prepare_and_eat(state, plan, WHEN, meal.slot, meal)
        leftover.portions[0].remaining_grams = 10.0  # touched from the Pantry page
        refresh_derived_fields(leftover, foods_by_id)
        assert not can_undo_preparation(state, plan, WHEN, meal.slot, meal)

    def test_undo_persist_failure_rolls_back(self, foods_by_id):
        state, plan, meal = self.prepared_with_leftover(foods_by_id)
        record_id = state.prepared_leftovers[0].id
        pantry_before = dict(state.pantry.items)
        state.fail_persist = True
        result = undo_preparation(state, plan, WHEN, meal.slot, meal)
        assert not result.ok
        assert state.pantry.items == pantry_before
        assert [lo.id for lo in state.prepared_leftovers] == [record_id]
        assert plan.tracking_entry(WHEN, meal.slot)["eaten"] is True
