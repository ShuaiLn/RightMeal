"""Pure costing layer tests: price_demand / price_slice and BudgetStatus.

The two pricing entry points share one per-food core, so full and slim
results can never drift; both are pure (no argument mutation, call-order
independent). BudgetStatus is priority-ordered: a known overage always wins
over missing price data.
"""

import copy

import pytest

from models import BudgetStatus, HouseholdProfile
from services.basket_builder import (
    GRAM_EPSILON, price_demand, price_slice,
)

from conftest import result_from_demand


@pytest.fixture
def profile():
    return HouseholdProfile(adults=1, city="Los Angeles", zip_code="90001")


DEMAND = {
    "rice_white": 1400.0,
    "chicken_breast": 900.0,
    "carrots": 700.0,
    "milk_whole": 2000.0,
}


class TestPurity:
    def test_price_demand_mutates_nothing(self, foods_by_id, seed_quotes):
        demand = dict(DEMAND)
        pantry = {"rice_white": 300.0}
        demand_before = copy.deepcopy(demand)
        pantry_before = copy.deepcopy(pantry)
        quotes_before = dict(seed_quotes)
        price_demand(demand, pantry, foods_by_id, seed_quotes)
        assert demand == demand_before
        assert pantry == pantry_before
        assert seed_quotes == quotes_before

    def test_price_slice_mutates_nothing(self, foods_by_id, seed_quotes):
        demand = dict(DEMAND)
        pantry = {"rice_white": 300.0}
        demand_before = copy.deepcopy(demand)
        price_slice(demand, pantry, foods_by_id, seed_quotes)
        assert demand == demand_before

    def test_call_order_independent(self, foods_by_id, seed_quotes):
        first = price_demand(DEMAND, {}, foods_by_id, seed_quotes)
        price_slice({"carrots": 100.0}, {}, foods_by_id, seed_quotes)
        second = price_demand(DEMAND, {}, foods_by_id, seed_quotes)
        assert first == second


class TestSliceEquivalence:
    def test_total_cost_identical_on_same_input(self, foods_by_id, seed_quotes):
        pantry = {"rice_white": 500.0}
        full = price_demand(DEMAND, pantry, foods_by_id, seed_quotes)
        slim = price_slice(DEMAND, pantry, foods_by_id, seed_quotes)
        assert slim.total_cost == full.total_cost
        assert slim.unpriced_gap_grams == full.unpriced_gap_grams
        assert slim.unpriced_food_ids == full.unpriced_food_ids

    def test_equivalence_with_unpriced_foods(self, foods_by_id, seed_quotes):
        quotes = {k: v for k, v in seed_quotes.items() if k != "chicken_breast"}
        full = price_demand(DEMAND, {}, foods_by_id, quotes)
        slim = price_slice(DEMAND, {}, foods_by_id, quotes)
        assert slim.total_cost == full.total_cost
        assert slim.unpriced_food_ids == full.unpriced_food_ids == {"chicken_breast"}


class TestGroupGrams:
    def test_deterministic_across_input_ordering(self, foods_by_id, seed_quotes):
        reordered = dict(reversed(list(DEMAND.items())))
        a = price_demand(DEMAND, {}, foods_by_id, seed_quotes)
        b = price_demand(reordered, {}, foods_by_id, seed_quotes)
        assert a.group_grams == b.group_grams
        assert a.items == b.items


class TestBudgetStatus:
    def test_within_under_full_coverage(self, foods_by_id, seed_quotes, nutrition, profile):
        result = result_from_demand(DEMAND, profile, 500.0, 7, foods_by_id, seed_quotes, nutrition)
        assert result.budget_status is BudgetStatus.WITHIN
        assert result.unpriced_food_names == ()

    def test_over_under_full_coverage(self, foods_by_id, seed_quotes, nutrition, profile):
        result = result_from_demand(DEMAND, profile, 1.0, 7, foods_by_id, seed_quotes, nutrition)
        assert result.budget_status is BudgetStatus.OVER
        assert any("exceeds" in msg for msg in result.relaxed_constraints)

    def test_known_over_wins_over_unpriced_gap(self, foods_by_id, seed_quotes, nutrition, profile):
        """Both facts surface at once: OVER status plus the missing-price note."""
        quotes = {k: v for k, v in seed_quotes.items() if k != "carrots"}
        result = result_from_demand(DEMAND, profile, 1.0, 7, foods_by_id, quotes, nutrition)
        assert result.budget_status is BudgetStatus.OVER
        assert any("exceeds" in msg for msg in result.relaxed_constraints)
        assert any("No price data" in msg for msg in result.relaxed_constraints)
        assert result.unpriced_food_names == (foods_by_id["carrots"].name,)

    def test_under_known_cost_but_gapped_is_unknown(
        self, foods_by_id, seed_quotes, nutrition, profile
    ):
        # The unpriced item is deliberately the expensive one: a small known
        # total must not read as WITHIN when the real cost is unknowable.
        quotes = {k: v for k, v in seed_quotes.items() if k != "chicken_breast"}
        result = result_from_demand(DEMAND, profile, 500.0, 7, foods_by_id, quotes, nutrition)
        assert result.budget_status is BudgetStatus.UNKNOWN
        assert any("No price data" in msg for msg in result.relaxed_constraints)
        assert foods_by_id["chicken_breast"].name in result.unpriced_food_names

    def test_float_residue_gap_stays_within(self, foods_by_id, seed_quotes, nutrition, profile):
        """A 1e-8 g residue on an unpriced food must not flip status to UNKNOWN."""
        demand = dict(DEMAND)
        demand["canola_oil"] = 1e-8  # unpriced below, but far under GRAM_EPSILON
        quotes = {k: v for k, v in seed_quotes.items() if k != "canola_oil"}
        result = result_from_demand(demand, profile, 500.0, 7, foods_by_id, quotes, nutrition)
        assert result.budget_status is BudgetStatus.WITHIN
        assert not any("No price data" in msg for msg in result.relaxed_constraints)

    def test_pantry_covers_everything_no_quotes_is_within(
        self, foods_by_id, nutrition, profile
    ):
        pantry = {fid: grams + 1.0 for fid, grams in DEMAND.items()}
        result = result_from_demand(
            DEMAND, profile, 10.0, 7, foods_by_id, None, nutrition, pantry=pantry
        )
        assert result.budget_status is BudgetStatus.WITHIN
        assert result.items == ()
        assert result.total_cost == 0.0

    def test_unpriced_message_truncates_after_three_names(
        self, foods_by_id, seed_quotes, nutrition, profile
    ):
        demand = {"rice_white": 500.0, "chicken_breast": 500.0, "carrots": 500.0,
                  "milk_whole": 500.0, "bananas": 500.0}
        result = result_from_demand(demand, profile, 500.0, 7, foods_by_id, {}, nutrition)
        assert result.budget_status is BudgetStatus.UNKNOWN
        message = next(m for m in result.relaxed_constraints if "No price data" in m)
        assert ", and 2 more" in message
        assert len(result.unpriced_food_names) == 5

    def test_gap_epsilon_boundary(self, foods_by_id, seed_quotes):
        """A gap must exceed GRAM_EPSILON to count as unpriced at all."""
        priced = price_demand({"carrots": GRAM_EPSILON}, {}, foods_by_id, {})
        assert priced.unpriced_gap_grams == 0.0
        assert priced.unpriced_food_ids == frozenset()
