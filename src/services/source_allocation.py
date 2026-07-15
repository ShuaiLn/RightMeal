"""Live ingredient-source allocation for the ACTIVE plan.

A plan's meals, portions, and planned nutrition are frozen once generated;
where the ingredients come from is not. This module re-derives, at render
time, how much of each food's remaining meal requirement is covered by the
live pantry and how much must still be bought (rounded up to packages and
priced) — so pantry edits are always reflected without re-running the
optimizer or changing any meal.

Contract:
- Pure functions; nothing here mutates the plan, the pantry, or stores.
- ``plan.pantry_used`` and ``plan.basket`` are the historical snapshot and
  are never rewritten.
- Only the ACTIVE plan (``end_date >= today``, current or future) may be
  rendered from these results. Historical plans (``is_historical``) keep
  their frozen snapshot — the planning view is the only caller and must
  branch on that.
- Completed = cooked, full stop: once a meal is prepared, its whole draw is
  done — leftovers are cooked food, never un-drawn raw ingredients. Neither
  ``used_fraction`` (eaten-vs-leftover split) nor ``pantry_deducted``
  (post-clamp stock audit) enters allocation; a stock-short preparation
  still completes its full requirement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from models.food import Food, Nutrients
from models.pricing import PriceSource
from models.pantry import Pantry
from models.plan import SavedBasketItem, SavedPlan
from services.pantry_flow import meal_draw_grams

# Sub-gram float dust (serialization rounds grams to 3 decimals) must never
# round a gap up into a whole extra package.
GRAM_EPSILON = 0.05


@dataclass(frozen=True)
class BuyLine:
    """Packages of one size still to buy for a food."""

    package_label: str
    package_grams: float  # per package — total grams = package_grams × count
    count: int
    est_cost: float
    source: str  # the original basket line's PriceSource value, or "seed"


@dataclass(frozen=True)
class FoodAllocation:
    """Where one food's frozen meal requirement is sourced from, live."""

    food_id: str
    meal_requirement: float  # Σ meal draws across the whole plan (frozen)
    completed: float  # requirement already cooked (prepared meals, full draw)
    from_pantry: float  # live stock covering the remaining requirement
    gap: float  # remaining requirement the pantry can't cover
    to_buy: tuple[BuyLine, ...]  # gap rounded up into packages
    covered: float  # completed + from_pantry, clamped to the requirement


def is_historical(plan: SavedPlan, today: date | None = None) -> bool:
    """Ended plans are frozen history; current AND future plans are live."""
    return plan.end_date < (today if today is not None else date.today())


def _requirements(plan: SavedPlan) -> tuple[dict[str, float], dict[str, float]]:
    """(meal_requirement, completed) per food, from the frozen meals and the
    tracking entries. Completed counts the FULL draw of prepared meals."""
    requirement: dict[str, float] = {}
    completed: dict[str, float] = {}
    for day in plan.meal_plan.days:
        when = plan.start_date + timedelta(days=day.day_index)
        for meal in day.meals:
            draws = meal_draw_grams(meal)
            if not draws:
                continue
            prepared = plan.tracking_entry(when, meal.slot).get("prepared") is True
            for food_id, grams in draws.items():
                requirement[food_id] = requirement.get(food_id, 0.0) + grams
                if prepared:
                    completed[food_id] = completed.get(food_id, 0.0) + grams
    return requirement, completed


def _fit_packages(
    food: Food, lines: list[SavedBasketItem], gap: float
) -> tuple[BuyLine, ...]:
    """Round a gram gap up into packages: original basket lines first (stored
    order, capped at their original counts — a fresh untouched plan reproduces
    the original basket exactly), then extra packages of the cheapest-per-gram
    original line; foods with no basket line use the cheapest-per-gram catalog
    package at its seed price."""
    if gap <= GRAM_EPSILON:
        return ()
    grams_by_label = {pkg.label: pkg.grams for pkg in food.package_options}
    remaining = gap
    order: list[str] = []
    acc: dict[str, dict] = {}  # label -> {grams, count, unit_cost, source}

    def add(label: str, grams: float, count: int, unit_cost: float, source: str) -> None:
        if count <= 0:
            return
        if label not in acc:
            acc[label] = {"grams": grams, "count": 0, "unit_cost": unit_cost, "source": source}
            order.append(label)
        acc[label]["count"] += count

    usable = [
        line for line in lines
        if line.count > 0 and grams_by_label.get(line.package_label, 0.0) > 0
    ]
    for line in usable:
        if remaining <= GRAM_EPSILON:
            break
        grams = grams_by_label[line.package_label]
        take = min(line.count, math.ceil((remaining - GRAM_EPSILON) / grams))
        if take <= 0:
            continue
        add(line.package_label, grams, take, line.cost / line.count, line.source)
        remaining -= take * grams

    if remaining > GRAM_EPSILON:
        if usable:
            top_up = min(
                usable,
                key=lambda ln: (ln.cost / ln.count) / grams_by_label[ln.package_label],
            )
            grams = grams_by_label[top_up.package_label]
            extra = math.ceil((remaining - GRAM_EPSILON) / grams)
            add(top_up.package_label, grams, extra, top_up.cost / top_up.count, top_up.source)
        else:
            packages = [pkg for pkg in food.package_options if pkg.grams > 0]
            if packages:
                cheapest = min(packages, key=lambda pkg: pkg.seed_price / pkg.grams)
                extra = math.ceil((remaining - GRAM_EPSILON) / cheapest.grams)
                add(
                    cheapest.label, cheapest.grams, extra, cheapest.seed_price,
                    PriceSource.SEED_ESTIMATE.value,
                )

    return tuple(
        BuyLine(
            package_label=label,
            package_grams=acc[label]["grams"],
            count=acc[label]["count"],
            est_cost=acc[label]["count"] * acc[label]["unit_cost"],
            source=acc[label]["source"],
        )
        for label in order
    )


def allocate_sources(
    plan: SavedPlan, pantry: Pantry, foods_by_id: dict[str, Food]
) -> dict[str, FoodAllocation]:
    """Live sourcing per food. Defensive clamps are part of the formulas —
    the invariants hold even against duplicated tracking entries or bad
    migrations, not just on the happy path."""
    requirement_raw, completed_raw = _requirements(plan)
    basket_by_food: dict[str, list[SavedBasketItem]] = {}
    for item in plan.basket:
        basket_by_food.setdefault(item.food_id, []).append(item)

    food_ids = (
        set(requirement_raw)
        | set(basket_by_food)
        | set(plan.purchased)
        | set(plan.pantry_used)
    )
    allocations: dict[str, FoodAllocation] = {}
    for food_id in sorted(food_ids):
        food = foods_by_id.get(food_id)
        if food is None:
            continue
        requirement = max(0.0, requirement_raw.get(food_id, 0.0))
        completed = min(requirement, max(0.0, completed_raw.get(food_id, 0.0)))
        remaining = max(0.0, requirement - completed)
        stock = max(0.0, pantry.items.get(food_id, 0.0))
        from_pantry = min(remaining, stock)
        gap = max(0.0, remaining - stock)
        if gap <= GRAM_EPSILON:
            gap = 0.0
        allocations[food_id] = FoodAllocation(
            food_id=food_id,
            meal_requirement=requirement,
            completed=completed,
            from_pantry=from_pantry,
            gap=gap,
            to_buy=_fit_packages(food, basket_by_food.get(food_id, []), gap),
            covered=min(requirement, completed + from_pantry),
        )
    return allocations


def covered_nutrients(
    allocations: dict[str, FoodAllocation], foods_by_id: dict[str, Food]
) -> Nutrients:
    """Nutrients of the covered grams — what current stock plus completed
    cooking secures of the plan's meals. Never counts pantry surplus or
    package overshoot beyond the meal requirement."""
    total = Nutrients()
    for food_id, allocation in allocations.items():
        food = foods_by_id.get(food_id)
        if food is not None and allocation.covered > 0:
            total = total.plus(
                food.nutrients_per_purchased_100g().scaled(allocation.covered / 100.0)
            )
    return total


def dynamic_open_cost(allocations: dict[str, FoodAllocation]) -> float:
    """Estimated cost of everything still to buy."""
    return sum(
        line.est_cost for allocation in allocations.values() for line in allocation.to_buy
    )
