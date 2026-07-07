"""Unit parsing and price normalization tests."""

import pytest

from services.units import normalized_price, parse_size, to_grams, to_ml


class TestParseSize:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1 lb", (1.0, "lb")),
            ("2.5 lb", (2.5, "lb")),
            ("16 oz", (16.0, "oz")),
            ("59 fl oz", (59.0, "fl oz")),
            ("1 gal", (1.0, "gal")),
            ("500 g", (500.0, "g")),
            ("1.5 kg", (1.5, "kg")),
            ("946 ml", (946.0, "ml")),
            ("12 ct", (12.0, "ct")),
            ("1 dozen", (1.0, "dozen")),
        ],
    )
    def test_parses(self, text, expected):
        assert parse_size(text) == expected

    @pytest.mark.parametrize("text", ["", "fresh", "$2.99"])
    def test_unparseable(self, text):
        assert parse_size(text) is None


class TestToGrams:
    def test_mass_units(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert to_grams(1, "lb", rice) == pytest.approx(453.592)
        assert to_grams(16, "oz", rice) == pytest.approx(453.592)
        assert to_grams(2, "kg", rice) == 2000

    def test_dozen_uses_curated_600g(self, foods_by_id):
        eggs = foods_by_id["eggs_large"]
        assert to_grams(1, "dozen", eggs) == pytest.approx(600)
        assert to_grams(2, "dozen", eggs) == pytest.approx(1200)

    def test_volume_needs_density(self, foods_by_id):
        milk = foods_by_id["milk_whole"]  # density 1.03
        assert to_grams(1, "gal", milk) == pytest.approx(3785.41 * 1.03)
        rice = foods_by_id["rice_white"]  # no density
        assert to_grams(1, "gal", rice) is None

    def test_unknown_unit(self, foods_by_id):
        assert to_grams(1, "bunch", foods_by_id["bananas"]) is None


class TestToMl:
    def test_volume_units(self, foods_by_id):
        milk = foods_by_id["milk_whole"]
        assert to_ml(1, "gal", milk) == pytest.approx(3785.41)
        assert to_ml(32, "fl oz", milk) == pytest.approx(946.352)

    def test_mass_to_ml_via_density(self, foods_by_id):
        oil = foods_by_id["canola_oil"]  # density 0.92
        assert to_ml(92, "g", oil) == pytest.approx(100)


class TestNormalizedPrice:
    def test_solid_per_100g(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        result = normalized_price(0.99, "1 lb", rice)
        assert result is not None
        per_100, unit = result
        assert unit == "100g"
        assert per_100 == pytest.approx(0.99 / 4.53592, rel=1e-4)

    def test_liquid_per_100ml(self, foods_by_id):
        milk = foods_by_id["milk_whole"]
        result = normalized_price(3.99, "1 gal", milk)
        assert result is not None
        per_100, unit = result
        assert unit == "100ml"
        assert per_100 == pytest.approx(3.99 / 37.8541, rel=1e-4)

    def test_unparseable_size_returns_none(self, foods_by_id):
        assert normalized_price(2.99, "family size", foods_by_id["rice_white"]) is None

    def test_dozen_price(self, foods_by_id):
        eggs = foods_by_id["eggs_large"]
        result = normalized_price(3.0, "1 dozen", eggs)
        assert result is not None
        per_100, unit = result
        assert unit == "100g"
        assert per_100 == pytest.approx(0.5)
