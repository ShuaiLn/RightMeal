"""Structural categorization of foods that didn't make it into the basket.

Categories come from collection membership only — never from parsing reason
strings: filter exclusions live in ``result.excluded_foods``, price coverage
in the quotes mapping, and everything else quoted-but-unpicked was simply not
selected by the optimizer.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping, Sequence

from models.basket import OptimizationResult
from models.food import Food
from models.pricing import PriceQuote

UNUSED_CATEGORY_LABELS: dict["UnusedCategory", str] = {}


class UnusedCategory(str, Enum):
    CANNOT_USE = "cannot_use"
    NOT_SELECTED = "not_selected"
    NO_RELIABLE_PRICE = "no_price"


UNUSED_CATEGORY_LABELS.update(
    {
        UnusedCategory.CANNOT_USE: "Can't use — dietary restrictions",
        UnusedCategory.NOT_SELECTED: "Not selected — didn't make the cut this time",
        UnusedCategory.NO_RELIABLE_PRICE: "No reliable price found",
    }
)


def categorize_unused(
    foods: Sequence[Food],
    result: OptimizationResult,
    quotes: Mapping[str, PriceQuote],
) -> dict[UnusedCategory, list[tuple[Food, str]]]:
    """Split every non-basket food into exactly one category (with a reason).

    Pantry-used foods count as "in the basket": the plan does use them, so they
    must never be listed as not selected."""
    basket_ids = {item.food.id for item in result.items} | {
        use.food.id for use in result.pantry_used
    }
    categories: dict[UnusedCategory, list[tuple[Food, str]]] = {
        UnusedCategory.CANNOT_USE: [],
        UnusedCategory.NOT_SELECTED: [],
        UnusedCategory.NO_RELIABLE_PRICE: [],
    }
    for food in foods:
        if food.id in basket_ids:
            continue
        if food.id in result.excluded_foods:
            categories[UnusedCategory.CANNOT_USE].append((food, result.excluded_foods[food.id]))
        elif food.id not in quotes:
            categories[UnusedCategory.NO_RELIABLE_PRICE].append(
                (food, "no usable price from any source")
            )
        else:
            categories[UnusedCategory.NOT_SELECTED].append(
                (food, f"not chosen within the ${result.budget:.0f} budget and nutrition targets")
            )
    return categories
