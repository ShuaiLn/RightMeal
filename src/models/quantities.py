"""Deterministic decimal normalization for persisted domain quantities.

User input and persisted numeric values enter the domain through these
helpers.  Calculations are performed with :class:`~decimal.Decimal` and only
then converted to floats where the rest of the nutrition/planning model still
uses floats.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

QUANTITY_QUANTUM = Decimal("0.001")
GRAMS_QUANTUM = Decimal("0.001")
MONEY_QUANTUM = Decimal("0.01")


def as_decimal(value: Any, label: str = "value") -> Decimal:
    """Parse a finite decimal without inheriting binary-float arithmetic."""

    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a finite number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be a finite number")
    return number


def quantize_decimal(
    value: Any,
    quantum: Decimal,
    *,
    label: str = "value",
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    number = as_decimal(value, label).quantize(quantum, rounding=ROUND_HALF_UP)
    if positive and number <= 0:
        raise ValueError(f"{label} must be positive")
    if non_negative and number < 0:
        raise ValueError(f"{label} must be non-negative")
    return number


def quantity_decimal(value: Any, *, positive: bool = True) -> Decimal:
    return quantize_decimal(
        value,
        QUANTITY_QUANTUM,
        label="quantity",
        positive=positive,
        non_negative=not positive,
    )


def grams_decimal(value: Any, *, positive: bool = False) -> Decimal:
    return quantize_decimal(
        value,
        GRAMS_QUANTUM,
        label="grams",
        positive=positive,
        non_negative=not positive,
    )


def money_decimal(value: Any, *, positive: bool = False) -> Decimal:
    return quantize_decimal(
        value,
        MONEY_QUANTUM,
        label="money",
        positive=positive,
        non_negative=not positive,
    )


def normalize_quantity(value: Any, *, positive: bool = True) -> float:
    return float(quantity_decimal(value, positive=positive))


def normalize_grams(value: Any, *, positive: bool = False) -> float:
    return float(grams_decimal(value, positive=positive))


def normalize_money(value: Any, *, positive: bool = False) -> float:
    return float(money_decimal(value, positive=positive))


def add_grams(left: Any, right: Any) -> float:
    return float(grams_decimal(as_decimal(left, "grams") + as_decimal(right, "grams")))


def subtract_grams(left: Any, right: Any) -> float:
    result = as_decimal(left, "grams") - as_decimal(right, "grams")
    if result < 0:
        result = Decimal("0")
    return float(grams_decimal(result))


def canonical_quantity(value: Any) -> str:
    return format(quantity_decimal(value), ".3f")


def canonical_grams(value: Any) -> str:
    return format(grams_decimal(value), ".3f")


def canonical_money(value: Any) -> str:
    return format(money_decimal(value), ".2f")
