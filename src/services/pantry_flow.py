"""Pure pantry-flow helpers: purchase events and meal consumption recording.

All functions mutate the passed plan/pantry/log in memory only — persistence
is the caller's job. Purchases are immutable PurchaseRecords (the source of
truth); ``plan.purchased`` is an aggregate cache rebuilt from them. Undo
VOIDS whole event groups and only when exact — it never touches stock the
user added or edited manually.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime
from typing import Sequence

from models.food import Food
from models.meals import Meal, MealSlot
from models.pantry import CUSTOM_ID_PREFIX, Pantry
from models.plan import RIGHTMEAL_NS, SavedBasketItem, SavedPlan
from models.prepared_leftover import (
    EPSILON,
    STATUS_AVAILABLE,
    STATUS_CONSUMED,
    PreparedLeftover,
    refresh_derived_fields,
    remaining_grams_map,
)
from models.purchase_log import (
    GRAMS_SOURCES,
    ORIGIN_LEGACY_MIGRATION,
    PRICE_SOURCE_UNKNOWN,
    PurchaseInput,
    PurchaseRecord,
)
from models.quantities import (
    add_grams,
    normalize_grams,
    normalize_money,
    normalize_quantity,
    subtract_grams,
)


def purchased_grams(food: Food, items: Sequence[SavedBasketItem]) -> float:
    """Total grams the plan buys of one food: count × package grams per line."""
    total = 0.0
    for item in items:
        if item.food_id == food.id:
            package_grams = item.package_grams
            if package_grams <= 0 and item.package_id:
                package = next(
                    (
                        package
                        for package in food.package_options
                        if package.package_id == item.package_id
                    ),
                    None,
                )
                package_grams = package.grams if package is not None else 0.0
            # Compatibility for in-memory pre-v6 constructors only. Persisted
            # ambiguous legacy rows retain no package identity or weight.
            if package_grams <= 0 and not item.package_id:
                candidates = [
                    package
                    for package in food.package_options
                    if package.label == item.package_label
                ]
                if len(candidates) == 1:
                    package_grams = candidates[0].grams
            total = add_grams(
                total,
                package_grams * item.count,
            )
    return total


# -- purchase events ---------------------------------------------------------


def _check_purchase_input(purchase_input: PurchaseInput) -> None:
    if not purchase_input.food_id:
        raise ValueError("a purchase event needs a catalog food_id")
    if purchase_input.food_id.startswith(CUSTOM_ID_PREFIX):
        # A pending custom pantry item is not a catalog food — it can never be
        # purchased, drawn, or counted until the user links it to a real food.
        raise ValueError("a custom pantry item cannot be purchased directly")
    if purchase_input.grams <= 0:
        raise ValueError("a purchase event needs positive grams")
    if purchase_input.grams_source not in GRAMS_SOURCES:
        raise ValueError("a purchase event needs a known grams source")
    if purchase_input.line_total is not None and purchase_input.line_total <= 0:
        raise ValueError("a confirmed item total must be positive")
    if normalize_quantity(purchase_input.quantity) <= 0:
        raise ValueError("a purchase event needs a positive quantity")
    if purchase_input.source_line_index is not None and purchase_input.source_line_index < 0:
        raise ValueError("source line index must be non-negative")
    if purchase_input.segment_index is not None and purchase_input.segment_index < 0:
        raise ValueError("segment index must be non-negative")


def _linked_basket_item(
    plan: SavedPlan | None,
    purchase_input: PurchaseInput,
) -> SavedBasketItem | None:
    """Resolve an explicit planned-child link; never infer one for imports."""

    if purchase_input.basket_item_id is None:
        return None
    if plan is None or not purchase_input.apply_to_plan:
        raise ValueError("a linked basket purchase must be applied to its plan")
    matches = [
        item
        for item in plan.basket
        if item.basket_item_id == purchase_input.basket_item_id
    ]
    if len(matches) != 1:
        raise ValueError("the referenced basket item does not exist uniquely")
    item = matches[0]
    if item.food_id != purchase_input.food_id:
        raise ValueError("the purchase food does not match the referenced basket item")
    if item.package_id is None or item.package_grams <= 0:
        raise ValueError("a display-only legacy basket item cannot be purchased directly")
    if purchase_input.package_id is not None and purchase_input.package_id != item.package_id:
        raise ValueError("the purchase package does not match the referenced basket item")
    if purchase_input.package_label and purchase_input.package_label != item.package_label:
        raise ValueError("the purchase package label does not match its basket snapshot")
    expected_grams = normalize_grams(
        item.package_grams * normalize_quantity(purchase_input.quantity),
        positive=True,
    )
    if abs(expected_grams - purchase_input.grams) > 0.05:
        raise ValueError("linked purchase grams do not match package snapshot and quantity")
    return item


def record_purchase_event(
    plan: SavedPlan | None,
    pantry: Pantry,
    log: list[PurchaseRecord],
    purchase_input: PurchaseInput,
    now: datetime | None = None,
) -> PurchaseRecord:
    """One purchase fact. The SERVICE reads the pantry baseline at mutation
    time and sets the timestamp — callers never construct either. Adds the
    grams to the pantry and refreshes the plan aggregate when applied.
    ``plan`` may be None (pantry photo with no plan): the event is off-plan."""
    _check_purchase_input(purchase_input)
    linked_item = _linked_basket_item(plan, purchase_input)
    applied = purchase_input.apply_to_plan and plan is not None
    estimated_line_cost = purchase_input.estimated_line_cost
    if estimated_line_cost is None and linked_item is not None:
        if normalize_quantity(purchase_input.quantity) == float(linked_item.count):
            estimated_line_cost = linked_item.cost
        else:
            estimated_line_cost = normalize_money(
                linked_item.unit_cost * normalize_quantity(purchase_input.quantity)
            )
    record = PurchaseRecord(
        event_id=purchase_input.event_id,
        food_id=purchase_input.food_id,
        raw_name=purchase_input.raw_name,
        brand=purchase_input.brand,
        package_label=(
            linked_item.package_label
            if linked_item is not None
            else purchase_input.package_label
        ),
        grams=normalize_grams(purchase_input.grams, positive=True),
        quantity=normalize_quantity(purchase_input.quantity),
        line_total=purchase_input.line_total,
        estimated_line_cost=estimated_line_cost,
        price_source=purchase_input.price_source,
        store=purchase_input.store,
        photo_path=purchase_input.photo_path,
        group_id=purchase_input.group_id or purchase_input.event_id,
        origin=purchase_input.origin,
        purchased_at=(now or datetime.now()).isoformat(timespec="seconds"),
        plan_id=plan.plan_id if applied else None,
        pantry_grams_before=normalize_grams(
            pantry.items.get(purchase_input.food_id, 0.0)
        ),
        grams_source=purchase_input.grams_source,
        source_line_index=purchase_input.source_line_index,
        segment_index=purchase_input.segment_index,
        currency=purchase_input.currency,
        basket_item_id=(
            linked_item.basket_item_id if linked_item is not None else None
        ),
        package_id=(
            linked_item.package_id
            if linked_item is not None
            else purchase_input.package_id
        ),
    )
    pantry.add(record.food_id, record.grams)
    log.append(record)
    if applied:
        rebuild_purchase_aggregates(plan, log)
    return record


def record_purchase_events(
    plan: SavedPlan | None,
    pantry: Pantry,
    log: list[PurchaseRecord],
    inputs: Sequence[PurchaseInput],
    now: datetime | None = None,
) -> list[PurchaseRecord]:
    """Sequential batch (multi-package button press, one receipt): validate
    everything first, then execute in order — each event's baseline reads the
    stock AFTER the previous one applied, so two same-food lines stack."""
    for purchase_input in inputs:
        _check_purchase_input(purchase_input)
        _linked_basket_item(plan, purchase_input)
    return [
        record_purchase_event(plan, pantry, log, purchase_input, now=now)
        for purchase_input in inputs
    ]


def _group_records(log: list[PurchaseRecord], group_id: str) -> list[PurchaseRecord]:
    return [rec for rec in log if rec.group_id == group_id and rec.voided_at is None]


def latest_group_for_food(log: list[PurchaseRecord], food_id: str) -> str | None:
    """The group of the food's most recent non-voided event — the only thing
    the plan page's Undo may target."""
    for record in reversed(log):
        if record.food_id == food_id and record.voided_at is None:
            return record.group_id
    return None


def can_void_group(
    plan: SavedPlan,
    pantry: Pantry,
    log: list[PurchaseRecord],
    group_id: str,
    today: date | None = None,
) -> tuple[bool, str]:
    """Whether the whole group can be undone exactly, with the user-facing
    reason when it can't. All-or-nothing: one unsafe record blocks the group."""
    records = _group_records(log, group_id)
    if not records:
        return False, "Nothing to undo."
    for record in records:
        if record.origin == ORIGIN_LEGACY_MIGRATION:
            return False, ("This purchase was migrated from an older plan and "
                           "cannot be safely undone.")
        if record.plan_id is not None:
            if record.plan_id != plan.plan_id:
                return False, "This purchase belongs to another plan."
            if plan.end_date < (today if today is not None else date.today()):
                return False, "This purchase belongs to a completed plan."
    group_ids = {record.event_id for record in records}
    per_food: dict[str, list[PurchaseRecord]] = {}
    for record in records:
        per_food.setdefault(record.food_id, []).append(record)
    for food_id, recs in per_food.items():
        food_events = [
            rec for rec in log if rec.food_id == food_id and rec.voided_at is None
        ]
        # Every record must sit at the TAIL of its food's event history —
        # a newer purchase (any origin, any plan) blocks the undo, because
        # baselines only unwind in reverse order.
        tail = food_events[-len(recs):]
        if any(rec.event_id not in group_ids for rec in tail):
            return False, "A newer pantry purchase exists — undo it first."
        stock = normalize_grams(pantry.items.get(food_id, 0.0))
        for record in reversed(tail):
            if stock - record.grams < record.pantry_grams_before - 1e-6:
                return False, "Some of this purchase has already been used."
            remaining = subtract_grams(stock, record.grams)
            stock = remaining
    return True, ""


def void_purchase_group(
    plan: SavedPlan,
    pantry: Pantry,
    log: list[PurchaseRecord],
    group_id: str,
    now: datetime | None = None,
    today: date | None = None,
) -> tuple[bool, str]:
    """Undo one whole user action (button press / receipt): mark every record
    voided — never delete — and remove exactly the recorded grams. Returns
    (ok, user-facing message when blocked)."""
    ok, message = can_void_group(plan, pantry, log, group_id, today=today)
    if not ok:
        return False, message
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    records = _group_records(log, group_id)
    ids = {record.event_id for record in records}
    for index, record in enumerate(log):
        if record.event_id in ids:
            log[index] = replace(record, voided_at=stamp)
    for record in records:
        pantry.remove(record.food_id, record.grams)
    if any(record.plan_id == plan.plan_id for record in records):
        rebuild_purchase_aggregates(plan, log)
    return True, ""


def rebuild_purchase_aggregates(plan: SavedPlan, log: list[PurchaseRecord]) -> None:
    """``plan.purchased`` is a CACHE: Σ non-voided applied event grams per
    food. Repairs the cache only — NEVER mutates the pantry (the grams were
    added when each event was recorded)."""
    totals: dict[str, float] = {}
    for record in log:
        if record.voided_at is None and record.plan_id == plan.plan_id:
            totals[record.food_id] = add_grams(
                totals.get(record.food_id, 0.0), record.grams
            )
    plan.purchased.clear()
    plan.purchased.update(totals)


def actual_spent(plan: SavedPlan, log: list[PurchaseRecord]) -> float:
    """Σ CONFIRMED line totals of this plan's non-voided purchases — every
    value passed through the editable confirm dialog. Unknown prices simply
    don't count; estimates never do."""
    return normalize_money(sum(
        record.line_total
        for record in log
        if record.plan_id == plan.plan_id
        and record.voided_at is None
        and record.line_total is not None
    ))


def purchased_value(
    plan: SavedPlan,
    log: list[PurchaseRecord],
    basket_items: Sequence[SavedBasketItem],
) -> float:
    """Confirmed totals plus estimates captured on actual purchase records.

    Planned costs are never inferred from a matching food id: buying one offer
    must not count every planned package/offer row for that food.
    ``basket_items`` remains in the signature for caller compatibility.
    """
    total = 0.0
    for record in log:
        if record.plan_id != plan.plan_id or record.voided_at is not None:
            continue
        if record.line_total is not None:
            total += record.line_total
        elif record.estimated_line_cost is not None:
            total += record.estimated_line_cost
    return normalize_money(total)


def migrate_legacy_purchases(
    plan: SavedPlan, log: list[PurchaseRecord]
) -> list[PurchaseRecord]:
    """Convert ``plan.purchased`` entries that predate the event log into
    synthetic records. Deterministic ids make retries idempotent; NEVER adds
    to the pantry — those grams landed when the purchase was checked off.
    Appends to ``log`` and returns the new records; the caller persists."""
    existing_ids = {record.event_id for record in log}
    foods_with_events = {
        record.food_id for record in log if record.plan_id == plan.plan_id
    }
    created: list[PurchaseRecord] = []
    for food_id, grams in sorted(plan.purchased.items()):
        if food_id in foods_with_events:
            continue
        event_id = str(uuid.uuid5(RIGHTMEAL_NS, f"{plan.plan_id}|{food_id}|legacy-purchased"))
        if event_id in existing_ids:
            continue
        record = PurchaseRecord(
            event_id=event_id,
            food_id=food_id,
            raw_name=food_id,
            brand=None,
            package_label=None,
            grams=normalize_grams(grams, positive=True),
            quantity=1.0,
            line_total=None,
            estimated_line_cost=None,
            price_source=PRICE_SOURCE_UNKNOWN,
            store="",
            photo_path=None,
            group_id=event_id,
            origin=ORIGIN_LEGACY_MIGRATION,
            purchased_at=plan.created_at,
            plan_id=plan.plan_id,
            pantry_grams_before=normalize_grams(
                plan.purchased_baseline.get(food_id, 0.0)
            ),
        )
        log.append(record)
        created.append(record)
    return created


def meal_draw_grams(meal: Meal) -> dict[str, float]:
    """Pantry grams cooking this meal consumes, per food.

    Mirrors the scheduler's supply draws: cooking a batch dinner consumes two
    servings at once (2× every portion), and its leftover lunch consumes
    nothing — the ingredients were already drawn with the dinner. A scheduled
    prepared leftover (ready meal) likewise draws nothing.
    """
    if meal.is_leftover or meal.prepared_leftover_id is not None:
        return {}
    draws: dict[str, float] = {}
    for portion in meal.portions:
        # Component-aware batch: a batch dinner cooks two servings of its MAIN
        # (the leftover lunch reuses it), but a fresh side is not batched.
        # Legacy portions default to component_kind="main", so their 2x draw is
        # unchanged.
        multiplier = 2.0 if (meal.batch_id and portion.component_kind == "main") else 1.0
        draws[portion.food.id] = draws.get(portion.food.id, 0.0) + portion.grams * multiplier
    return draws


def mark_ingredients_used(
    plan: SavedPlan,
    pantry: Pantry,
    when: date,
    slot: MealSlot,
    meal: Meal,
    fraction: float,
) -> None:
    """Deduct ``fraction`` of the meal's ingredient draw from the pantry (clamped
    to available stock) and record the post-clamp actuals for exact undo."""
    deducted: dict[str, float] = {}
    for food_id, grams in meal_draw_grams(meal).items():
        removed = pantry.remove(food_id, grams * fraction)
        if removed > 0:
            deducted[food_id] = removed
    plan.set_ingredients_used(when, slot, fraction, deducted)


def undo_ingredients_used(plan: SavedPlan, pantry: Pantry, when: date, slot: MealSlot) -> None:
    """Add back exactly what marking deducted, then clear the marked state."""
    entry = plan.tracking_entry(when, slot)
    for food_id, grams in dict(entry.get("pantry_deducted", {})).items():
        pantry.add(food_id, grams)
    plan.clear_ingredients_used(when, slot)


def eat_prepared_leftover(
    plan: SavedPlan,
    leftovers_by_id: dict[str, PreparedLeftover],
    foods_by_id: dict[str, Food],
    when: date,
    slot: MealSlot,
    leftover_id: str,
) -> tuple[float, float]:
    """Consume this meal's reserved servings from a prepared leftover.

    Never touches the pantry — cooked food is not raw stock. Returns
    (reserved, consumed); consumed < reserved means the leftover went stale
    (eaten or discarded from the Pantry page) and the meal is eaten anyway.
    Records the pre-consumption per-food snapshot and the actual per-food
    grams removed, so undo is exact even if the record is edited later.
    """
    reserved = plan.leftovers_used.get(leftover_id, 0.0)
    leftover = leftovers_by_id.get(leftover_id)
    available = (
        leftover.servings_remaining
        if leftover is not None and leftover.status == STATUS_AVAILABLE
        else 0.0
    )
    consumed = min(available, reserved)
    if leftover is None or consumed <= EPSILON:
        # Stale: still record that this meal's consumption happened (0 servings)
        # so it can never be double-consumed after a display-status correction.
        plan.set_leftover_consumption(when, slot, 0.0, {}, {})
        return reserved, 0.0
    before = remaining_grams_map(leftover)
    factor = consumed / leftover.servings_remaining
    consumed_grams: dict[str, float] = {}
    for portion in leftover.portions:
        delta = portion.remaining_grams * factor
        if delta > 0:
            consumed_grams[portion.food_id] = consumed_grams.get(portion.food_id, 0.0) + delta
            portion.remaining_grams -= delta
    refresh_derived_fields(leftover, foods_by_id)  # flips to consumed at ~0
    plan.set_leftover_consumption(when, slot, consumed, consumed_grams, before)
    return reserved, consumed


def undo_prepared_leftover(
    plan: SavedPlan,
    leftovers_by_id: dict[str, PreparedLeftover],
    foods_by_id: dict[str, Food],
    when: date,
    slot: MealSlot,
    leftover_id: str,
) -> None:
    """Restore exactly the recorded per-food grams — capped by the
    pre-consumption snapshot so intervening edits can never inflate stock."""
    entry = plan.tracking_entry(when, slot)
    consumed_grams = entry.get("leftover_consumed_grams") or {}
    before_grams = entry.get("leftover_before_grams") or {}
    leftover = leftovers_by_id.get(leftover_id)
    if leftover is not None and consumed_grams:
        for portion in leftover.portions:
            add = consumed_grams.get(portion.food_id, 0.0)
            cap = before_grams.get(portion.food_id, portion.original_grams)
            portion.remaining_grams = min(portion.remaining_grams + add, cap)
        if leftover.status == STATUS_CONSUMED:
            leftover.status = STATUS_AVAILABLE
        refresh_derived_fields(leftover, foods_by_id)
    plan.set_leftover_consumption(when, slot, None, None, None)
