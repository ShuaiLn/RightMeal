"""PreparedLeftover model + store: derived servings, tolerant persistence."""

import json

import pytest

from models.prepared_leftover import (
    STATUS_AVAILABLE,
    STATUS_CONSUMED,
    PreparedFoodPortion,
    PreparedLeftover,
    derive_remaining_fraction,
    refresh_derived_fields,
    remaining_grams_map,
    suggested_use_by,
)
from services.prepared_leftovers_store import PreparedLeftoversStore

pytestmark = []


def make_leftover(foods_by_id, portions=None, **overrides) -> PreparedLeftover:
    if portions is None:
        portions = [
            PreparedFoodPortion("rice_white", "White rice", 200.0, 100.0),
            PreparedFoodPortion("chicken_breast", "Chicken breast", 300.0, 0.0),
        ]
    fields = dict(
        id="lo1",
        origin_kind="user",
        source_date="2026-07-10",
        source_slot="dinner",
        source_meal_template_id="tmpl",
        meal_name="Chicken fried rice",
        note="left about a third",
        initial_fraction_remaining=0.33,
        servings_remaining=0.0,
        portions=portions,
        prepared_at="2026-07-10",
        use_by_date="2026-07-13",
        created_at="2026-07-10T20:00:00",
        status=STATUS_AVAILABLE,
    )
    fields.update(overrides)
    leftover = PreparedLeftover(**fields)
    refresh_derived_fields(leftover, foods_by_id)
    return leftover


class TestDerivation:
    def test_kcal_weighted_not_grams_weighted(self, foods_by_id):
        rice_kcal = foods_by_id["rice_white"].nutrients_per_purchased_100g().calories_kcal
        chicken_kcal = foods_by_id["chicken_breast"].nutrients_per_purchased_100g().calories_kcal
        leftover = make_leftover(foods_by_id)
        expected = (rice_kcal * 100.0 / 100.0) / (
            rice_kcal * 200.0 / 100.0 + chicken_kcal * 300.0 / 100.0
        )
        assert leftover.servings_remaining == pytest.approx(expected, abs=1e-9)
        # Distinct from the grams-weighted value (100 / 500 = 0.2) unless the
        # kcal densities happened to be equal.
        assert rice_kcal != chicken_kcal

    def test_grams_weighted_fallback_when_no_nutrition(self, foods_by_id):
        portions = [PreparedFoodPortion("not_a_food", "Mystery", 400.0, 100.0)]
        assert derive_remaining_fraction(portions, foods_by_id) == pytest.approx(0.25)

    def test_refresh_clamps_grams_and_flips_empty_to_consumed(self, foods_by_id):
        leftover = make_leftover(foods_by_id)
        leftover.portions[0].remaining_grams = -3.0
        leftover.portions[1].remaining_grams = 999.0  # above original 300
        refresh_derived_fields(leftover, foods_by_id)
        assert leftover.portions[0].remaining_grams == 0.0
        assert leftover.portions[1].remaining_grams == 300.0
        leftover.portions[0].remaining_grams = 0.0
        leftover.portions[1].remaining_grams = 0.0
        refresh_derived_fields(leftover, foods_by_id)
        assert leftover.servings_remaining == 0.0
        assert leftover.status == STATUS_CONSUMED

    def test_initial_fraction_untouched_by_consumption(self, foods_by_id):
        leftover = make_leftover(foods_by_id)
        for p in leftover.portions:
            p.remaining_grams *= 0.5
        refresh_derived_fields(leftover, foods_by_id)
        assert leftover.initial_fraction_remaining == pytest.approx(0.33)

    def test_remaining_grams_map_aggregates_by_food(self, foods_by_id):
        portions = [
            PreparedFoodPortion("rice_white", "White rice", 100.0, 40.0),
            PreparedFoodPortion("rice_white", "White rice", 50.0, 10.0),
        ]
        leftover = make_leftover(foods_by_id, portions=portions)
        assert remaining_grams_map(leftover) == pytest.approx({"rice_white": 50.0})

    def test_suggested_use_by_is_three_days_out(self):
        from datetime import date

        assert suggested_use_by(date(2026, 7, 10)) == "2026-07-13"


class TestPersistence:
    def test_round_trip(self, tmp_path, foods_by_id):
        store = PreparedLeftoversStore(tmp_path)
        original = make_leftover(foods_by_id)
        store.save([original])
        loaded = store.load(foods_by_id)
        assert len(loaded) == 1
        got = loaded[0]
        assert got.id == original.id
        assert got.meal_name == original.meal_name
        assert got.note == original.note
        assert got.initial_fraction_remaining == pytest.approx(0.33)
        assert got.use_by_date == "2026-07-13"
        assert [p.remaining_grams for p in got.portions] == [
            p.remaining_grams for p in original.portions
        ]
        assert got.servings_remaining == pytest.approx(original.servings_remaining)

    def test_load_rederives_servings_ignoring_tampered_cache(self, tmp_path, foods_by_id):
        store = PreparedLeftoversStore(tmp_path)
        leftover = make_leftover(foods_by_id)
        store.save([leftover])
        data = json.loads(store.path.read_text(encoding="utf-8"))
        data["items"][0]["servings_remaining"] = 0.99  # hand-edited lie
        store.path.write_text(json.dumps(data), encoding="utf-8")
        got = store.load(foods_by_id)[0]
        assert got.servings_remaining == pytest.approx(leftover.servings_remaining)
        assert got.servings_remaining != pytest.approx(0.99)

    def test_malformed_records_dropped_individually(self, tmp_path, foods_by_id):
        store = PreparedLeftoversStore(tmp_path)
        good = make_leftover(foods_by_id)
        data = json.loads(store.to_json_text([good]))
        data["items"].append({"id": "broken"})  # no portions
        data["items"].append("not even a dict")
        store.base_dir.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps(data), encoding="utf-8")
        loaded = store.load(foods_by_id)
        assert [lo.id for lo in loaded] == ["lo1"]

    def test_missing_and_corrupt_files_load_empty(self, tmp_path, foods_by_id):
        store = PreparedLeftoversStore(tmp_path)
        assert store.load(foods_by_id) == []
        store.base_dir.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{oops", encoding="utf-8")
        assert store.load(foods_by_id) == []

    def test_bad_status_drops_record(self, tmp_path, foods_by_id):
        store = PreparedLeftoversStore(tmp_path)
        leftover = make_leftover(foods_by_id)
        data = json.loads(store.to_json_text([leftover]))
        data["items"][0]["status"] = "eaten_by_dog"
        store.base_dir.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps(data), encoding="utf-8")
        assert store.load(foods_by_id) == []

    def test_delete(self, tmp_path, foods_by_id):
        store = PreparedLeftoversStore(tmp_path)
        store.save([make_leftover(foods_by_id)])
        store.delete()
        assert store.load(foods_by_id) == []
        store.delete()  # idempotent
