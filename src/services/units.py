"""Unit parsing and price normalization.

Providers report sizes like "16 oz", "1 gal", "12 ct"; the optimizer compares
everything as cost per 100 g (solids) or per 100 ml (liquids). Liquids convert
between mass and volume only when the food's density is known; otherwise the
curated seed conversions are used instead (handled by callers).
"""

from __future__ import annotations

import re
import math

from models.food import Food
from services.package_units import package_unit

GRAMS_PER: dict[str, float] = {
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "oz": 28.3495,
    "ounce": 28.3495,
    "ounces": 28.3495,
    "lb": 453.592,
    "lbs": 453.592,
    "pound": 453.592,
    "pounds": 453.592,
}

ML_PER: dict[str, float] = {
    "ml": 1.0,
    "milliliter": 1.0,
    "milliliters": 1.0,
    "l": 1000.0,
    "liter": 1000.0,
    "liters": 1000.0,
    "fl oz": 29.5735,
    "floz": 29.5735,
    "gal": 3785.41,
    "gallon": 3785.41,
    "gallons": 3785.41,
    "qt": 946.353,
    "quart": 946.353,
    "quarts": 946.353,
    "pt": 473.176,
    "pint": 473.176,
    "pints": 473.176,
}

COUNT_UNITS = ("dozen", "doz", "ct", "count", "each", "ea")

_SIZE_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>fl\s*oz|[a-zA-Z]+)",
    re.IGNORECASE,
)


def parse_size(text: str) -> tuple[float, str] | None:
    """Parse a size string like '2.5 lb' or '59 fl oz' into (value, unit)."""
    if not text:
        return None
    if re.search(r"\bhalf\s+dozen\b", text, re.IGNORECASE):
        return 0.5, "dozen"
    match = _SIZE_RE.search(text)
    if not match:
        return None
    unit = re.sub(r"\s+", " ", match.group("unit").strip().lower())
    if unit == "floz":
        unit = "fl oz"
    return float(match.group("value")), unit


def to_grams(value: float, unit: str, food: Food) -> float | None:
    """Convert a (value, unit) size to grams for the given food, if possible."""
    unit = unit.lower()
    if unit in GRAMS_PER:
        return value * GRAMS_PER[unit]
    if unit in ML_PER:
        if food.density_g_per_ml:
            return value * ML_PER[unit] * food.density_g_per_ml
        return None
    if unit in ("dozen", "doz"):
        per_dozen = _package_grams_matching(food, "dozen")
        if per_dozen is not None:
            return value * per_dozen
        return None
    if unit in ("ct", "count", "each", "ea"):
        per_count = _grams_per_count(food)
        if per_count is not None:
            return value * per_count
        return None
    return None


def _grams_per_count(food: Food) -> float | None:
    """Grams per single countable unit, derived from curated packages."""
    per_dozen = _package_grams_matching(food, "dozen")
    if per_dozen is not None:
        return per_dozen / 12.0
    candidates: list[float] = []
    for pkg in food.package_options:
        size = parse_size(pkg.label)
        if size and size[1] in ("ct", "count", "each", "ea"):
            candidates.append(float(package_unit(food, pkg).grams) / size[0])
    if not candidates or any(
        not math.isclose(value, candidates[0], abs_tol=0.001)
        for value in candidates[1:]
    ):
        return None
    return candidates[0]


def to_ml(value: float, unit: str, food: Food) -> float | None:
    """Convert a (value, unit) size to milliliters for the given food, if possible."""
    unit = unit.lower()
    if unit in ML_PER:
        return value * ML_PER[unit]
    if unit in GRAMS_PER:
        if food.density_g_per_ml:
            return value * GRAMS_PER[unit] / food.density_g_per_ml
        return None
    return None


def _package_grams_matching(food: Food, keyword: str) -> float | None:
    candidates: list[float] = []
    for pkg in food.package_options:
        if keyword in pkg.label.lower():
            # e.g. eggs "1 dozen" -> 600 g per dozen (curated spec conversion)
            size = parse_size(pkg.label)
            count = size[0] if size else 1.0
            # Bind and revalidate before using catalog package grams.
            candidates.append(float(package_unit(food, pkg).grams) / count)
    if not candidates or any(
        not math.isclose(value, candidates[0], abs_tol=0.001)
        for value in candidates[1:]
    ):
        return None
    return candidates[0]


def normalized_price(price: float, size_text: str, food: Food) -> tuple[float, str] | None:
    """Normalize a product price to (cost per 100, '100g'|'100ml').

    Returns None when the size cannot be parsed or converted — callers must
    treat that as "no usable unit price" and fall through to the next provider.
    """
    if price < 0:
        return None
    size = parse_size(size_text)
    if size is None:
        return None
    value, unit = size
    if value <= 0:
        return None
    if food.is_liquid:
        ml = to_ml(value, unit, food)
        if ml is None or ml <= 0:
            return None
        return price / (ml / 100.0), "100ml"
    grams = to_grams(value, unit, food)
    if grams is None or grams <= 0:
        return None
    return price / (grams / 100.0), "100g"
