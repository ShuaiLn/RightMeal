"""Local weight and price resolution for evidence-only photo facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from models.food import Food, PackageOption
from models.photo_analysis import (
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
    GRAMS_SOURCES,
)
from services.pantry_matcher import normalize_identity

OUNCE_GRAMS = 28.349523125
POUND_GRAMS = 453.59237
MILLILITERS = {
    "ml": 1.0,
    "milliliter": 1.0,
    "milliliters": 1.0,
    "l": 1000.0,
    "liter": 1000.0,
    "liters": 1000.0,
    "fl oz": 29.5735295625,
    "fluid ounce": 29.5735295625,
    "fluid ounces": 29.5735295625,
    "gal": 3785.411784,
    "gallon": 3785.411784,
    "gallons": 3785.411784,
    "qt": 946.352946,
    "quart": 946.352946,
    "quarts": 946.352946,
    "pt": 473.176473,
    "pint": 473.176473,
    "pints": 473.176473,
}

_WEIGHT_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>fl\s*oz|fluid\s+ounces?|milliliters?|ml|liters?|kg|kilograms?|"
    r"grams?|g|ounces?|oz|pounds?|lbs?|gallons?|gal|quarts?|qt|pints?|pt|l)\b",
    re.IGNORECASE,
)
_MULTIPACK_RE = re.compile(r"(?P<count>\d+)\s*(?:x|×)\s*", re.IGNORECASE)
_OFFER_RE = re.compile(r"\b(?P<count>\d+)\s+for\s+[$€£]?\s*\d", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedWeight:
    grams: float | None
    source: str | None
    label: str
    package_options: tuple[PackageOption, ...] = ()


def convert_to_grams(
    value: float,
    unit: str,
    *,
    density_g_per_ml: float | None = None,
) -> float | None:
    """Convert a supported mass or volume unit without inventing density."""

    if value <= 0:
        return None
    normalized = re.sub(r"\s+", " ", unit.strip().casefold())
    if normalized in ("g", "gram", "grams"):
        return value
    if normalized in ("kg", "kilogram", "kilograms"):
        return value * 1000.0
    if normalized in ("oz", "ounce", "ounces"):
        return value * OUNCE_GRAMS
    if normalized in ("lb", "lbs", "pound", "pounds"):
        return value * POUND_GRAMS
    milliliters = MILLILITERS.get(normalized)
    if milliliters is None or density_g_per_ml is None or density_g_per_ml <= 0:
        return None
    return value * milliliters * density_g_per_ml


def weight_fact_to_grams(
    fact: WeightFact | None,
    *,
    density_g_per_ml: float | None,
) -> float | None:
    if fact is None:
        return None
    return convert_to_grams(
        fact.value,
        fact.unit,
        density_g_per_ml=density_g_per_ml,
    )


def parse_description_grams(
    text: str,
    *,
    density_g_per_ml: float | None = None,
) -> float | None:
    """Parse one visible package expression, including ``2 x 16 oz``."""

    match = _WEIGHT_RE.search(text)
    if match is None:
        return None
    value = float(match.group("value").replace(",", "."))
    prefix = text[:match.start()]
    multipack = list(_MULTIPACK_RE.finditer(prefix))
    count = int(multipack[-1].group("count")) if multipack else 1
    grams = convert_to_grams(
        value,
        match.group("unit"),
        density_g_per_ml=density_g_per_ml,
    )
    return grams * count if grams is not None else None


def matching_packages(description: str, food: Food) -> tuple[PackageOption, ...]:
    normalized = normalize_identity(description)
    if not normalized:
        return ()
    matches = []
    for package in food.package_options:
        label = normalize_identity(package.label)
        if label and (label in normalized or normalized in label):
            matches.append(package)
    return tuple(matches)


def resolve_weight(
    facts: ProductFacts | ReceiptLineFacts,
    food: Food | None,
) -> ResolvedWeight:
    """Resolve grams in the exact evidence-to-estimate precedence order."""

    density = food.density_g_per_ml if food is not None else None
    total = weight_fact_to_grams(facts.total_weight, density_g_per_ml=density)
    if total is not None:
        return ResolvedWeight(total, GRAMS_SOURCE_VISIBLE_TOTAL, "Visible total weight")

    unit = weight_fact_to_grams(facts.unit_weight, density_g_per_ml=density)
    if unit is not None and facts.quantity is not None:
        return ResolvedWeight(
            unit * facts.quantity,
            GRAMS_SOURCE_VISIBLE_UNIT_TIMES_QUANTITY,
            "Visible unit weight multiplied by quantity",
        )

    if isinstance(facts, ProductFacts):
        description = " ".join(filter(None, [facts.package_text, facts.observed_name]))
    else:
        description = facts.raw_printed_text
    parsed = parse_description_grams(description, density_g_per_ml=density)
    if parsed is not None:
        return ResolvedWeight(
            parsed,
            GRAMS_SOURCE_DESCRIPTION_PARSED,
            "Weight parsed from visible description",
        )

    packages = matching_packages(description, food) if food is not None else ()
    if len(packages) == 1:
        return ResolvedWeight(
            packages[0].grams,
            GRAMS_SOURCE_CATALOG_ESTIMATE,
            f"Catalog estimate: {packages[0].label}",
            packages,
        )
    return ResolvedWeight(
        None,
        None,
        "Weight requires confirmation",
        packages if packages else (food.package_options if food is not None else ()),
    )


def user_entered_weight(grams: float) -> ResolvedWeight:
    if grams <= 0:
        raise ValueError("User-entered grams must be positive.")
    return ResolvedWeight(grams, GRAMS_SOURCE_USER_ENTERED, "User-entered grams")


def confirmed_line_total(line: ReceiptLineFacts) -> float | None:
    """Return only a positive printed merchandise line total allowed by policy."""

    if (
        line.classification is not ReceiptLineClassification.MERCHANDISE
        or line.printed_line_total is None
        or line.printed_line_total <= 0
    ):
        return None
    offer = _OFFER_RE.search(line.raw_printed_text)
    if offer is not None:
        applied_quantity = int(offer.group("count"))
        if line.quantity != applied_quantity:
            return None
    return line.printed_line_total


def confirmed_item_spend(lines: Sequence[ReceiptLineFacts]) -> float:
    return sum(value for line in lines if (value := confirmed_line_total(line)) is not None)
