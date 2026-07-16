"""Food-bound package selection, conversion, and display helpers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from models.food import Food, PackageOption, PackageUnit
from models.plan import SavedPlan
from models.purchase_log import PurchaseRecord
from models.quantities import grams_decimal, quantity_decimal
from services.source_allocation import is_historical


def package_unit(food: Food, package: PackageOption | str) -> PackageUnit:
    label = package if isinstance(package, str) else package.label
    for option in food.package_options:
        if option.label == label:
            if isinstance(package, PackageOption) and grams_decimal(option.grams) != grams_decimal(
                package.grams
            ):
                break
            return PackageUnit.from_option(food, option)
    raise ValueError("the package does not belong to the selected food")


def package_quantity_to_grams(
    quantity: object,
    food: Food,
    unit: PackageUnit,
) -> float:
    return unit.to_grams(quantity, food)


def units_for_labels(food: Food, labels: Iterable[str]) -> tuple[PackageUnit, ...]:
    wanted = set(labels)
    return tuple(
        PackageUnit.from_option(food, option)
        for option in food.package_options
        if option.label in wanted
    )


def plan_package_units(
    food: Food,
    plan: SavedPlan | None,
    *,
    today: date | None = None,
) -> tuple[PackageUnit, ...]:
    if plan is None or is_historical(plan, today=today):
        return ()
    return units_for_labels(
        food,
        (item.package_label for item in plan.basket if item.food_id == food.id),
    )


def recent_purchase_package_unit(
    food: Food,
    log: Sequence[PurchaseRecord],
) -> PackageUnit | None:
    for record in reversed(log):
        if (
            record.food_id != food.id
            or record.voided_at is not None
            or not record.package_label
        ):
            continue
        try:
            return package_unit(food, record.package_label)
        except ValueError:
            continue
    return None


def preferred_package_unit(
    food: Food,
    plan: SavedPlan | None,
    log: Sequence[PurchaseRecord],
    *,
    today: date | None = None,
) -> PackageUnit | None:
    """Plan packages win; recent purchases break a multi-package tie."""

    planned = plan_package_units(food, plan, today=today)
    recent = recent_purchase_package_unit(food, log)
    if planned:
        if recent is not None:
            for unit in planned:
                if unit == recent:
                    return unit
        return max(planned, key=lambda unit: unit.grams)
    return recent


def package_unit_name(unit: PackageUnit) -> str:
    """A label suitable beside an amount field (``1 dozen`` -> ``dozen``)."""

    label = unit.package_label.strip()
    if label.casefold().startswith("1 "):
        return label[2:].strip()
    return label


def package_amount(grams: object, unit: PackageUnit) -> Decimal:
    return quantity_decimal(grams_decimal(grams) / unit.grams, positive=False)


def compact_decimal(value: Decimal) -> str:
    text = format(value, ".3f").rstrip("0").rstrip(".")
    return text or "0"


def format_grams(
    food: Food,
    grams: object,
    unit: PackageUnit | None = None,
) -> str:
    normalized = grams_decimal(grams)
    if unit is not None:
        if unit.food_id != food.id:
            raise ValueError("the package unit belongs to a different food")
        # Revalidate against current catalog data before presentation too.
        unit.option_for(food)
        return f"{compact_decimal(package_amount(normalized, unit))} {package_unit_name(unit)}"
    value = float(normalized)
    if food.is_liquid and food.density_g_per_ml:
        ml = value / food.density_g_per_ml
        return f"{ml / 1000:.1f} L" if ml >= 1000 else f"{ml:.0f} ml"
    label = f"{value / 1000:.1f} kg" if value >= 1000 else f"{value:.0f} g"
    if food.form == "dry":
        label += " dry"
    return label


def display_amount(food: Food, grams: object, unit: PackageUnit | None) -> float:
    if unit is not None:
        unit.option_for(food)
        return float(package_amount(grams, unit))
    normalized = float(grams_decimal(grams))
    if food.is_liquid and food.density_g_per_ml:
        return normalized / food.density_g_per_ml
    return normalized


def display_amount_to_grams(
    food: Food,
    amount: object,
    unit: PackageUnit | None,
) -> float:
    if unit is not None:
        return unit.to_grams(amount, food)
    value = quantity_decimal(amount, positive=False)
    if food.is_liquid and food.density_g_per_ml:
        return float(grams_decimal(value * Decimal(str(food.density_g_per_ml))))
    return float(grams_decimal(value))


def base_unit_name(food: Food) -> str:
    return "ml" if food.is_liquid and food.density_g_per_ml else "g"
