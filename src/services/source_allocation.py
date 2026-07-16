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
from models.quantities import money_decimal
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
    basket_item_id: str | None = None
    package_id: str | None = None
    offer_id: str | None = None
    unit_cost_cents: int = 0
    total_cost_cents: int = 0
    store: str = ""


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

    packages_by_id = {package.package_id: package for package in food.package_options}

    def snapshot_grams(line: SavedBasketItem) -> float:
        if line.package_grams > 0:
            return line.package_grams
        if line.package_id and line.package_id in packages_by_id:
            return packages_by_id[line.package_id].grams
        # Only supports old in-memory constructors. A persisted ambiguous v6
        # row has neither id nor snapshot and therefore remains display-only.
        if not line.package_id:
            matches = [
                package
                for package in food.package_options
                if package.label == line.package_label
            ]
            if len(matches) == 1:
                return matches[0].grams
        return 0.0

    remaining = gap
    order: list[str] = []
    acc: dict[str, dict] = {}

    def add(
        identity: str,
        *,
        label: str,
        grams: float,
        count: int,
        unit_cost_cents: int,
        source: str,
        store: str,
        basket_item_id: str | None,
        package_id: str | None,
        offer_id: str | None,
    ) -> None:
        if count <= 0:
            return
        if identity not in acc:
            acc[identity] = {
                "label": label,
                "grams": grams,
                "count": 0,
                "unit_cost_cents": unit_cost_cents,
                "source": source,
                "store": store,
                "basket_item_id": basket_item_id,
                "package_id": package_id,
                "offer_id": offer_id,
            }
            order.append(identity)
        acc[identity]["count"] += count

    usable = [(line, snapshot_grams(line)) for line in lines if line.count > 0]
    usable = [(line, grams) for line, grams in usable if grams > 0]
    for line, grams in usable:
        if remaining <= GRAM_EPSILON:
            break
        take = min(line.count, math.ceil((remaining - GRAM_EPSILON) / grams))
        if take <= 0:
            continue
        add(
            line.basket_item_id,
            label=line.package_label,
            grams=grams,
            count=take,
            unit_cost_cents=line.unit_cost_cents,
            source=line.source,
            store=line.store,
            basket_item_id=line.basket_item_id,
            package_id=line.package_id,
            offer_id=line.offer_id,
        )
        remaining -= take * grams

    if remaining > GRAM_EPSILON:
        if usable:
            top_up, grams = min(
                usable,
                key=lambda pair: (
                    pair[0].unit_cost_cents / pair[1],
                    pair[0].basket_item_id,
                ),
            )
            extra = math.ceil((remaining - GRAM_EPSILON) / grams)
            add(
                top_up.basket_item_id,
                label=top_up.package_label,
                grams=grams,
                count=extra,
                unit_cost_cents=top_up.unit_cost_cents,
                source=top_up.source,
                store=top_up.store,
                basket_item_id=top_up.basket_item_id,
                package_id=top_up.package_id,
                offer_id=top_up.offer_id,
            )
        else:
            packages = [package for package in food.package_options if package.grams > 0]
            if packages:
                cheapest = min(
                    packages,
                    key=lambda package: (
                        package.seed_price / package.grams,
                        package.package_id,
                    ),
                )
                extra = math.ceil((remaining - GRAM_EPSILON) / cheapest.grams)
                unit_cost_cents = int(money_decimal(cheapest.seed_price) * 100)
                add(
                    f"catalog:{cheapest.package_id}",
                    label=cheapest.label,
                    grams=cheapest.grams,
                    count=extra,
                    unit_cost_cents=unit_cost_cents,
                    source=PriceSource.SEED_ESTIMATE.value,
                    store="Seed data",
                    basket_item_id=None,
                    package_id=cheapest.package_id,
                    offer_id=None,
                )

    result: list[BuyLine] = []
    for identity in order:
        row = acc[identity]
        total_cost_cents = row["count"] * row["unit_cost_cents"]
        result.append(
            BuyLine(
                package_label=row["label"],
                package_grams=row["grams"],
                count=row["count"],
                est_cost=total_cost_cents / 100.0,
                source=row["source"],
                basket_item_id=row["basket_item_id"],
                package_id=row["package_id"],
                offer_id=row["offer_id"],
                unit_cost_cents=row["unit_cost_cents"],
                total_cost_cents=total_cost_cents,
                store=row["store"],
            )
        )
    return tuple(result)


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
