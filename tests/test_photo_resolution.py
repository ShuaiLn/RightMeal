import pytest

from models.photo_analysis import (
    BoundingRegion,
    FoodForm,
    ProductFacts,
    ReceiptLineClassification,
    ReceiptLineFacts,
    WeightFact,
)
from models.purchase_log import (
    GRAMS_SOURCE_CATALOG_ESTIMATE,
    GRAMS_SOURCE_DESCRIPTION_PARSED,
    GRAMS_SOURCE_USER_ENTERED,
    GRAMS_SOURCE_VISIBLE_TOTAL,
    GRAMS_SOURCE_VISIBLE_UNIT_TIMES_QUANTITY,
)
from services.photo_resolution import (
    confirmed_item_spend,
    confirmed_line_total,
    convert_to_grams,
    parse_description_grams,
    resolve_weight,
    user_entered_weight,
)


def product(total=None, unit=None, quantity=2, package=""):
    return ProductFacts(
        observed_name="Product",
        generic_food_name="food",
        brand=None,
        language="en",
        form=FoodForm.UNKNOWN,
        package_text=package,
        quantity=quantity,
        total_weight=total,
        unit_weight=unit,
        printed_price=None,
        printed_currency=None,
        visible_evidence=(),
    )


def receipt_line(text, total=5.0, quantity=1, classification=None):
    return ReceiptLineFacts(
        source_line_index=0,
        bounding_region=BoundingRegion(0.1, 0.1, 0.9, 0.2),
        raw_printed_text=text,
        generic_item_name="food",
        brand=None,
        language="en",
        form=FoodForm.UNKNOWN,
        quantity=quantity,
        total_weight=None,
        unit_weight=None,
        printed_line_total=total,
        classification=classification or ReceiptLineClassification.MERCHANDISE,
    )


@pytest.mark.parametrize(("value", "unit", "expected"), [
    (100, "g", 100),
    (1, "kg", 1000),
    (1, "oz", 28.349523125),
    (1, "lb", 453.59237),
])
def test_mass_conversions(value, unit, expected):
    assert convert_to_grams(value, unit) == pytest.approx(expected)


@pytest.mark.parametrize(("unit", "milliliters"), [
    ("ml", 1), ("L", 1000), ("fl oz", 29.5735295625),
    ("gal", 3785.411784), ("qt", 946.352946), ("pt", 473.176473),
])
def test_volume_conversions_require_catalog_density(unit, milliliters):
    assert convert_to_grams(1, unit) is None
    assert convert_to_grams(1, unit, density_g_per_ml=1.03) == pytest.approx(
        milliliters * 1.03
    )


def test_weight_precedence_and_sources(foods_by_id):
    rice = foods_by_id["rice_white"]
    total = WeightFact(900, "g", "900 g")
    unit = WeightFact(500, "g", "500 g")
    result = resolve_weight(product(total, unit, package="2 x 16 oz"), rice)
    assert result.grams == 900
    assert result.source == GRAMS_SOURCE_VISIBLE_TOTAL

    result = resolve_weight(product(None, unit, quantity=2), rice)
    assert result.grams == 1000
    assert result.source == GRAMS_SOURCE_VISIBLE_UNIT_TIMES_QUANTITY

    result = resolve_weight(product(None, None, package="2 x 16 oz"), rice)
    assert result.grams == pytest.approx(2 * 16 * 28.349523125)
    assert result.source == GRAMS_SOURCE_DESCRIPTION_PARSED


def test_unique_catalog_package_is_labeled_estimate(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    package = eggs.package_options[0]
    result = resolve_weight(product(package=package.label), eggs)
    assert result.grams == package.grams
    assert result.source == GRAMS_SOURCE_CATALOG_ESTIMATE
    assert result.label.startswith("Catalog estimate")


def test_user_entered_source_and_multipack_parser():
    assert user_entered_weight(250).source == GRAMS_SOURCE_USER_ENTERED
    assert parse_description_grams("NET WT 2 × 16 oz") == pytest.approx(
        2 * 16 * 28.349523125
    )
    with pytest.raises(ValueError):
        user_entered_weight(0)


def test_price_policy_accepts_only_positive_merchandise_extended_total():
    ordinary = receipt_line("MILK 3.50", total=3.5)
    applied_offer = receipt_line("2 FOR $5", total=5.0, quantity=2)
    unapplied_offer = receipt_line("2 FOR $5", total=5.0, quantity=1)
    coupon = receipt_line(
        "LOYALTY -1.00",
        total=1.0,
        classification=ReceiptLineClassification.LOYALTY_DISCOUNT,
    )
    tax = receipt_line("TAX 0.30", total=0.3, classification=ReceiptLineClassification.TAX)
    assert confirmed_line_total(ordinary) == 3.5
    assert confirmed_line_total(applied_offer) == 5.0
    assert confirmed_line_total(unapplied_offer) is None
    assert confirmed_line_total(coupon) is None
    assert confirmed_line_total(tax) is None
    assert confirmed_item_spend([ordinary, applied_offer, coupon, tax]) == 8.5
