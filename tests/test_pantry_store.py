"""Pantry model and persistence tests using a temporary directory."""

import json

import pytest

from models import Pantry
from services.pantry_store import PantryStore


class TestPantryModel:
    def test_add_accumulates(self):
        pantry = Pantry()
        pantry.add("brown_rice", 1000.0)
        pantry.add("brown_rice", 500.0)
        assert pantry.items == {"brown_rice": 1500.0}

    def test_add_ignores_non_positive(self):
        pantry = Pantry()
        pantry.add("brown_rice", 0.0)
        pantry.add("brown_rice", -5.0)
        assert pantry.items == {}

    def test_remove_clamps_at_zero_and_reports_actual(self):
        pantry = Pantry(items={"brown_rice": 300.0})
        assert pantry.remove("brown_rice", 1000.0) == pytest.approx(300.0)
        assert "brown_rice" not in pantry.items
        # removing from an absent food is a no-op
        assert pantry.remove("brown_rice", 100.0) == 0.0

    def test_remove_partial_keeps_remainder(self):
        pantry = Pantry(items={"brown_rice": 300.0})
        assert pantry.remove("brown_rice", 100.0) == pytest.approx(100.0)
        assert pantry.items["brown_rice"] == pytest.approx(200.0)

    def test_set_grams_zero_removes(self):
        pantry = Pantry(items={"brown_rice": 300.0})
        pantry.set_grams("brown_rice", 0.0)
        assert pantry.items == {}
        pantry.set_grams("brown_rice", 250.0)
        assert pantry.items == {"brown_rice": 250.0}


class TestPantryStore:
    def test_first_run_returns_empty_pantry(self, tmp_path, foods_by_id):
        pantry = PantryStore(tmp_path).load(foods_by_id)
        assert isinstance(pantry, Pantry)
        assert pantry.items == {}

    def test_round_trip(self, tmp_path, foods_by_id):
        store = PantryStore(tmp_path)
        food_id = next(iter(foods_by_id))
        pantry = Pantry(items={food_id: 1234.5})
        store.save(pantry)
        loaded = store.load(foods_by_id)
        assert loaded.items == {food_id: 1234.5}

    def test_corrupt_file_returns_empty(self, tmp_path, foods_by_id):
        store = PantryStore(tmp_path)
        store.base_dir.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json", encoding="utf-8")
        assert store.load(foods_by_id).items == {}

    def test_version_mismatch_returns_empty(self, tmp_path, foods_by_id):
        store = PantryStore(tmp_path)
        food_id = next(iter(foods_by_id))
        store.save(Pantry(items={food_id: 100.0}))
        data = json.loads(store.path.read_text(encoding="utf-8"))
        data["version"] = 999
        store.path.write_text(json.dumps(data), encoding="utf-8")
        assert store.load(foods_by_id).items == {}

    def test_unknown_ids_and_non_positive_grams_dropped(self, tmp_path, foods_by_id):
        store = PantryStore(tmp_path)
        food_id = next(iter(foods_by_id))
        store.base_dir.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": {food_id: 500.0, "no_such_food": 100.0, "also_bad": -3.0},
                }
            ),
            encoding="utf-8",
        )
        assert store.load(foods_by_id).items == {food_id: 500.0}

    def test_delete_idempotent(self, tmp_path, foods_by_id):
        store = PantryStore(tmp_path)
        food_id = next(iter(foods_by_id))
        store.save(Pantry(items={food_id: 100.0}))
        store.delete()
        assert store.load(foods_by_id).items == {}
        store.delete()  # no error on second delete
