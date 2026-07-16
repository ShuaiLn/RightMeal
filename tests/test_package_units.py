"""Food-bound package and decimal normalization contracts."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from models.food import PackageOption
from models.pantry import Pantry
from models.purchase_log import PurchaseInput
from models.quantities import (
    canonical_grams,
    canonical_money,
    canonical_quantity,
    normalize_quantity,
)
from services.package_units import (
    display_amount_to_grams,
    format_grams,
    package_unit,
    package_quantity_to_grams,
    preferred_package_unit,
)
from services.pantry_flow import record_purchase_event
from services.units import to_grams


def test_named_eggs_package_is_food_bound_and_decimal_safe(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    dozen = package_unit(eggs, "1 dozen")
    assert dozen.food_id == "eggs_large"
    assert dozen.package_label == "1 dozen"
    assert package_quantity_to_grams("0.5", eggs, dozen) == 300.0
    assert format_grams(eggs, 300, dozen) == "0.5 dozen"
    assert display_amount_to_grams(eggs, "0.5", dozen) == 300.0


def test_other_food_uses_its_own_dozen_and_missing_dozen_is_not_guessed(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    synthetic = replace(
        eggs,
        id="synthetic_dozen_food",
        package_options=(PackageOption("1 dozen", 720, 4.0),),
    )
    assert to_grams(1, "dozen", synthetic) == 720.0

    without_dozen = replace(
        eggs,
        id="no_dozen_food",
        package_options=(PackageOption("500 g bag", 500, 2.0),),
    )
    assert to_grams(1, "dozen", without_dozen) is None


def test_package_from_previous_food_is_immediately_invalid(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    rice = foods_by_id["rice_white"]
    stale = package_unit(eggs, "1 dozen")
    with pytest.raises(ValueError, match="different food"):
        stale.to_grams(1, rice)


def test_decimal_canonicalization_and_pantry_math_are_stable():
    assert {normalize_quantity(value) for value in ("1", "1.0", "1.00")} == {1.0}
    assert canonical_quantity("1") == "1.000"
    assert canonical_grams("1") == "1.000"
    assert canonical_money("1") == "1.00"

    pantry = Pantry()
    pantry.add("food", 0.1)
    pantry.add("food", 0.2)
    assert pantry.items["food"] == 0.3
    assert pantry.to_dict()["items"]["food"] == "0.300"


def test_recent_valid_purchase_package_is_used_without_a_plan(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    pantry, log = Pantry(), []
    record_purchase_event(None, pantry, log, PurchaseInput(
        event_id="eggs-half",
        food_id=eggs.id,
        package_label="half dozen",
        grams=300,
        quantity=1,
    ))
    selected = preferred_package_unit(eggs, None, log)
    assert selected is not None and selected.package_label == "half dozen"
    assert format_grams(eggs, pantry.items[eggs.id], selected) == "1 half dozen"

    plan = SimpleNamespace(
        end_date=date(2026, 7, 20),
        basket=(
            SimpleNamespace(food_id=eggs.id, package_label="1 dozen"),
            SimpleNamespace(food_id=eggs.id, package_label="half dozen"),
        ),
    )
    assert preferred_package_unit(
        eggs, plan, [], today=date(2026, 7, 14)
    ).package_label == "1 dozen"
    assert preferred_package_unit(
        eggs, plan, log, today=date(2026, 7, 14)
    ).package_label == "half dozen"
