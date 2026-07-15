"""Pure-logic tests for planning_view helpers (no Flet rendering)."""

from services.source_allocation import GRAM_EPSILON
from ui.planning_view import pantry_coverage_note


class TestPantryCoverageNote:
    def test_zero_from_pantry_is_none(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert pantry_coverage_note(rice, 0.0) is None

    def test_below_epsilon_is_none(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert pantry_coverage_note(rice, GRAM_EPSILON) is None

    def test_dry_grams_food_label(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert pantry_coverage_note(rice, 200.0) == "200 g dry covered by pantry"

    def test_liquid_food_label(self, foods_by_id):
        milk = foods_by_id["milk_whole"]
        assert pantry_coverage_note(milk, 103.0) == "100 ml covered by pantry"
