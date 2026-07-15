"""Prepared leftovers in plan generation: the pre-pass that pins ready meals.

(The legacy-scheduler pinning and demand-offset tests were removed with the
legacy engine; recipe-first pinning is covered in test_recipe_engine.)
"""

import logging
from datetime import date, timedelta

import pytest

from models import HouseholdProfile, MealSlot
from models.prepared_leftover import (
    PreparedFoodPortion,
    PreparedLeftover,
    refresh_derived_fields,
)
from planner import (
    assign_prepared_leftovers,
    household_slot_targets,
    preassigned_nutrients,
)
from services.pantry_flow import meal_draw_grams

START = date(2026, 7, 7)


@pytest.fixture(scope="module")
def profile():
    return HouseholdProfile(adults=2, children=2, city="Los Angeles", zip_code="90001")


@pytest.fixture(scope="module")
def slot_targets(nutrition, profile):
    return household_slot_targets(nutrition, profile)


def substantial_grams(foods_by_id, slot_targets) -> dict[str, float]:
    """Rice + chicken grams sized programmatically to clear the lunch gate
    (60% kcal, 50% protein) with headroom."""
    lunch = slot_targets[MealSlot.LUNCH]
    rice = foods_by_id["rice_white"]
    chicken = foods_by_id["chicken_breast"]
    rice_kcal_pg = rice.nutrients_per_purchased_100g().calories_kcal / 100.0
    chicken_protein_pg = chicken.nutrients_per_purchased_100g().protein_g / 100.0
    return {
        "rice_white": 1.2 * 0.6 * lunch.calories_kcal / rice_kcal_pg,
        "chicken_breast": 1.2 * 0.5 * lunch.protein_g / chicken_protein_pg,
    }


def make_leftover(foods_by_id, grams: dict[str, float], leftover_id="lo1",
                  prepared=START, shelf_days=3, status="available",
                  remaining_scale=1.0, **overrides) -> PreparedLeftover:
    fields = dict(
        id=leftover_id,
        origin_kind="user",
        source_date=prepared.isoformat(),
        source_slot="dinner",
        source_meal_template_id="grain_protein_bowl",
        meal_name="Chicken rice bowl",
        note="",
        initial_fraction_remaining=1.0,
        servings_remaining=0.0,
        portions=[
            PreparedFoodPortion(fid, fid, g, g * remaining_scale)
            for fid, g in sorted(grams.items())
        ],
        prepared_at=prepared.isoformat(),
        use_by_date=(prepared + timedelta(days=shelf_days)).isoformat(),
        created_at="2026-07-07T20:00:00",
        status=status,
    )
    fields.update(overrides)
    leftover = PreparedLeftover(**fields)
    refresh_derived_fields(leftover, foods_by_id)
    return leftover


class TestPrepass:
    def test_substantial_leftover_takes_earliest_lunch(self, foods_by_id, slot_targets):
        leftover = make_leftover(foods_by_id, substantial_grams(foods_by_id, slot_targets))
        assignments = assign_prepared_leftovers(
            [leftover], 7, START, foods_by_id, slot_targets
        )
        assert len(assignments) == 1
        a = assignments[0]
        assert (a.day_index, a.slot) == (0, MealSlot.LUNCH)
        assert a.meal.name == "Leftovers: Chicken rice bowl"
        assert a.meal.is_leftover and a.meal.batch_id is None
        assert a.meal.prepared_leftover_id == leftover.id
        assert a.servings_used == pytest.approx(1.0)
        assert meal_draw_grams(a.meal) == {}  # ready meals draw no raw supply

    def test_never_breakfast_and_second_leftover_takes_dinner(self, foods_by_id, slot_targets):
        grams = substantial_grams(foods_by_id, slot_targets)
        lo1 = make_leftover(foods_by_id, grams, "a1")
        lo2 = make_leftover(foods_by_id, grams, "a2")
        assignments = assign_prepared_leftovers([lo1, lo2], 7, START, foods_by_id, slot_targets)
        assert [(a.day_index, a.slot) for a in assignments] == [
            (0, MealSlot.LUNCH), (0, MealSlot.DINNER),
        ]
        assert all(a.slot is not MealSlot.BREAKFAST for a in assignments)

    def test_ordering_soonest_use_by_first_then_id(self, foods_by_id, slot_targets):
        grams = substantial_grams(foods_by_id, slot_targets)
        later = make_leftover(foods_by_id, grams, "b-later", shelf_days=3)
        sooner = make_leftover(foods_by_id, grams, "z-sooner", shelf_days=1)
        assignments = assign_prepared_leftovers(
            [later, sooner], 7, START, foods_by_id, slot_targets
        )
        assert [a.leftover_id for a in assignments] == ["z-sooner", "b-later"]
        tied1 = make_leftover(foods_by_id, grams, "tie-b")
        tied2 = make_leftover(foods_by_id, grams, "tie-a")
        tied = assign_prepared_leftovers([tied1, tied2], 7, START, foods_by_id, slot_targets)
        assert [a.leftover_id for a in tied] == ["tie-a", "tie-b"]  # id tie-break

    def test_expired_and_non_available_excluded(self, foods_by_id, slot_targets):
        grams = substantial_grams(foods_by_id, slot_targets)
        expired = make_leftover(foods_by_id, grams, "old", prepared=START - timedelta(days=10))
        discarded = make_leftover(foods_by_id, grams, "disc", status="discarded")
        assert assign_prepared_leftovers(
            [expired, discarded], 7, START, foods_by_id, slot_targets
        ) == []

    def test_not_scheduled_past_use_by(self, foods_by_id, slot_targets):
        grams = substantial_grams(foods_by_id, slot_targets)
        # use-by = day 0: both day-0 slots taken by fresher competitors would
        # push it out of its window entirely. Here: three leftovers, first two
        # (sooner use-by) take day 0; the third also expires day 0 -> unplaced.
        lo1 = make_leftover(foods_by_id, grams, "a", shelf_days=0)
        lo2 = make_leftover(foods_by_id, grams, "b", shelf_days=0)
        lo3 = make_leftover(foods_by_id, grams, "c", shelf_days=0)
        assignments = assign_prepared_leftovers(
            [lo1, lo2, lo3], 7, START, foods_by_id, slot_targets
        )
        assert [a.leftover_id for a in assignments] == ["a", "b"]
        assert all(a.day_index == 0 for a in assignments)

    def test_not_scheduled_before_prepared_at(self, foods_by_id, slot_targets):
        """A dish cooked on day 1 must not land on day 0, even with day 0 free."""
        grams = substantial_grams(foods_by_id, slot_targets)
        leftover = make_leftover(foods_by_id, grams, prepared=START + timedelta(days=1))
        assignments = assign_prepared_leftovers([leftover], 7, START, foods_by_id, slot_targets)
        assert len(assignments) == 1
        assert (assignments[0].day_index, assignments[0].slot) == (1, MealSlot.LUNCH)

    def test_unparseable_dates_skip_with_warning_not_today(self, foods_by_id, slot_targets,
                                                           caplog):
        grams = substantial_grams(foods_by_id, slot_targets)
        broken = make_leftover(foods_by_id, grams, prepared_at="soonish", use_by_date="")
        with caplog.at_level(logging.WARNING, logger="planner.leftover_prepass"):
            assignments = assign_prepared_leftovers(
                [broken], 7, START, foods_by_id, slot_targets
            )
        assert assignments == []
        assert any("unparseable" in record.message for record in caplog.records)

    def test_thin_leftover_fails_nutrition_gate(self, foods_by_id, slot_targets):
        """Half a bowl is not a meal: it stays in the store for manual eating."""
        thin = make_leftover(foods_by_id, {"rice_white": 60.0}, "thin")
        assert assign_prepared_leftovers([thin], 7, START, foods_by_id, slot_targets) == []

    def test_portions_use_remaining_grams_not_original(self, foods_by_id, slot_targets):
        grams = {fid: g * 2 for fid, g in substantial_grams(foods_by_id, slot_targets).items()}
        leftover = make_leftover(foods_by_id, grams, remaining_scale=0.5)
        assignments = assign_prepared_leftovers([leftover], 7, START, foods_by_id, slot_targets)
        assert len(assignments) == 1
        by_food = {p.food.id: p.grams for p in assignments[0].meal.portions}
        for fid, g in grams.items():
            assert by_food[fid] == pytest.approx(g * 0.5)

    def test_finished_component_contributes_nothing_to_offset(self, foods_by_id, slot_targets):
        """'The chicken is gone': its protein must not reduce purchases."""
        grams = substantial_grams(foods_by_id, slot_targets)
        full = make_leftover(foods_by_id, grams, "full")
        no_chicken = make_leftover(foods_by_id, grams, "nochx")
        for p in no_chicken.portions:
            if p.food_id == "chicken_breast":
                p.remaining_grams = 0.0
        refresh_derived_fields(no_chicken, foods_by_id)
        offset_full = preassigned_nutrients(
            assign_prepared_leftovers([full], 7, START, foods_by_id, slot_targets)
        )
        assignments = assign_prepared_leftovers([no_chicken], 7, START, foods_by_id, slot_targets)
        if assignments:  # may fail the protein gate entirely, which is also honest
            offset = preassigned_nutrients(assignments)
            chicken_protein = (
                foods_by_id["chicken_breast"].nutrients_per_purchased_100g().protein_g
                / 100.0 * grams["chicken_breast"]
            )
            assert offset.protein_g == pytest.approx(
                offset_full.protein_g - chicken_protein, rel=1e-6
            )
        else:
            assert offset_full.protein_g > 0

    def test_unknown_food_ids_dropped_all_unknown_skipped(self, foods_by_id, slot_targets):
        grams = substantial_grams(foods_by_id, slot_targets)
        mixed = make_leftover(foods_by_id, grams, "mixed")
        mixed.portions.append(PreparedFoodPortion("no_such_food", "?", 100.0, 100.0))
        ghost = make_leftover(foods_by_id, grams, "ghost")
        ghost.portions = [PreparedFoodPortion("no_such_food", "?", 100.0, 100.0)]
        ghost.servings_remaining = 1.0  # bypass derive: simulate a stale record
        assignments = assign_prepared_leftovers(
            [ghost, mixed], 7, START, foods_by_id, slot_targets
        )
        assert [a.leftover_id for a in assignments] == ["mixed"]
        assert all(
            p.food.id != "no_such_food" for a in assignments for p in a.meal.portions
        )
        # the unresolvable record did not consume the lunch slot
        assert assignments[0].slot is MealSlot.LUNCH

