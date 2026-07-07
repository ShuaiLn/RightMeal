"""Model contract tests: enums, labels, nutrient math, quote validation."""

import pytest

from models import (
    PRICE_SOURCE_LABELS,
    FoodGroup,
    Nutrients,
    PrepState,
    PriceQuote,
    PriceSource,
)


class TestEnums:
    def test_price_source_values(self):
        assert {s.value for s in PriceSource} == {
            "kroger_real_price",
            "instacart_numeric_price",
            "bls_regional_average",
            "seed_estimate",
        }

    def test_price_source_labels_exact(self):
        assert PRICE_SOURCE_LABELS[PriceSource.KROGER_REAL_PRICE] == "Kroger/Ralphs real price"
        assert PRICE_SOURCE_LABELS[PriceSource.INSTACART_NUMERIC_PRICE] == "Instacart numeric product price"
        assert PRICE_SOURCE_LABELS[PriceSource.BLS_REGIONAL_AVERAGE] == "BLS regional average estimate"
        assert PRICE_SOURCE_LABELS[PriceSource.SEED_ESTIMATE] == "Seed estimate"

    def test_every_source_has_a_label(self):
        assert set(PRICE_SOURCE_LABELS) == set(PriceSource)

    def test_prep_state_values(self):
        assert {s.value for s in PrepState} == {"raw", "cooked", "canned", "prepared"}

    def test_food_groups_are_six(self):
        assert len(FoodGroup) == 6


class TestNutrients:
    def test_scaled_and_plus(self):
        n = Nutrients(calories_kcal=100, protein_g=10)
        doubled = n.scaled(2)
        assert doubled.calories_kcal == 200
        assert doubled.protein_g == 20
        total = doubled.plus(Nutrients(calories_kcal=50, iron_mg=3))
        assert total.calories_kcal == 250
        assert total.iron_mg == 3
        assert total.protein_g == 20

    def test_names_cover_all_fields(self):
        assert len(Nutrients.NAMES) == 12
        n = Nutrients()
        assert set(n.as_dict()) == set(Nutrients.NAMES)

    def test_from_dict_rejects_unknown_fields(self):
        with pytest.raises(ValueError):
            Nutrients.from_dict({"calories_kcal": 1, "sodium_mg": 2})

    def test_every_nutrient_has_a_label(self):
        assert set(Nutrients.NUTRIENT_LABELS) == set(Nutrients.NAMES)


def _quote(**overrides) -> PriceQuote:
    base = dict(
        food_name="Eggs, large",
        matched_product_name="Grade A Large Eggs",
        price=3.2,
        unit="1 dozen",
        unit_price=3.2,
        normalized_unit_price=0.53,
        raw_unit="dozen",
        normalized_unit="100g",
        store="Seed data",
        source=PriceSource.SEED_ESTIMATE,
        confidence=1.0,
        is_estimate=True,
        last_updated="2026-01-01T00:00:00",
        match_reason="curated seed estimate",
    )
    base.update(overrides)
    return PriceQuote(**base)


class TestPriceQuote:
    def test_valid_quote(self):
        q = _quote()
        assert q.source is PriceSource.SEED_ESTIMATE
        assert q.provider_error is None

    def test_source_must_be_enum(self):
        with pytest.raises(ValueError):
            _quote(source="kroger_real_price_typo")

    def test_normalized_unit_restricted(self):
        with pytest.raises(ValueError):
            _quote(normalized_unit="per_lb")
        _quote(normalized_unit="100ml")  # ok

    def test_confidence_bounds(self):
        with pytest.raises(ValueError):
            _quote(confidence=1.5)
        with pytest.raises(ValueError):
            _quote(confidence=-0.1)
