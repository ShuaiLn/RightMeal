"""Domain operations for meal tracking: prepare/eat, leftovers, and undo.

Every operation follows one pattern: snapshot the in-memory objects, apply
the pure mutation, persist all three stores in ONE transaction, and restore
the snapshot if the save fails. UI handlers only call these functions and
rebuild on success / show the returned message on failure — they never touch
stores directly.

Two separate facts, deliberately not conflated:
- ``prepared`` — ingredients were deducted (or a leftover was consumed) for
  this meal. This is the inventory fact and it survives display corrections.
- ``eaten`` — what the card shows. "Correct display status" clears it while
  keeping ``prepared``, so re-clicking Eaten can never deduct twice.
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Protocol

from models.food import Food
from models.meals import Meal, MealSlot
from models.pantry import Pantry
from models.plan import SavedPlan
from models.prepared_leftover import (
    COMPONENT_BOTH,
    COMPONENT_MAIN,
    COMPONENT_SIDE,
    EPSILON,
    ORIGIN_BATCH,
    ORIGIN_USER,
    STATUS_AVAILABLE,
    STATUS_CONSUMED,
    PreparedFoodPortion,
    PreparedLeftover,
    derive_remaining_fraction,
    refresh_derived_fields,
    suggested_use_by,
)
from services.pantry_flow import (
    eat_prepared_leftover,
    mark_ingredients_used,
    undo_ingredients_used,
    undo_prepared_leftover,
)

logger = logging.getLogger(__name__)

# When the AI's overall fraction disagrees with its own per-component answers
# by more than this, the derived (component-based) value wins for display too.
OVERALL_MISMATCH_TOLERANCE = 0.15

STALE_LEFTOVER_MESSAGE = "That leftover was already used up — the meal is marked eaten anyway."
SAVE_FAILED_MESSAGE = "Couldn't save — the change was rolled back."


class TrackingState(Protocol):
    """The slice of AppState these operations need (structural, for tests)."""

    pantry: Pantry
    prepared_leftovers: list[PreparedLeftover]

    @property
    def foods_by_id(self) -> dict[str, Food]: ...

    @property
    def leftovers_by_id(self) -> dict[str, PreparedLeftover]: ...

    def persist(self, *, plan=None, pantry=None, leftovers=None) -> None: ...


@dataclass(frozen=True)
class TrackingResult:
    ok: bool
    message: str | None = None


def meal_was_prepared(entry: dict) -> bool:
    """Whether inventory was already affected for this meal — derived from the
    recorded facts for legacy entries that lack the explicit flag."""
    return (
        bool(entry.get("prepared"))
        or bool(entry.get("pantry_deducted"))
        or entry.get("leftover_consumed") is not None
    )


def _leftover_ref(entry: dict, meal: Meal) -> str | None:
    """The prepared-leftover record this meal consumes when eaten, if any."""
    return meal.prepared_leftover_id or entry.get("linked_leftover_id")


# -- commit helper --------------------------------------------------------


def _snapshot(state: TrackingState, saved: SavedPlan) -> dict:
    return {
        "pantry_items": dict(state.pantry.items),
        "tracking": copy.deepcopy(saved.tracking),
        "leftovers": copy.deepcopy(state.prepared_leftovers),
        "leftovers_used": dict(saved.leftovers_used),
    }


def _restore(state: TrackingState, saved: SavedPlan, snap: dict) -> None:
    state.pantry.items.clear()
    state.pantry.items.update(snap["pantry_items"])
    saved.tracking.clear()
    saved.tracking.update(snap["tracking"])
    state.prepared_leftovers[:] = snap["leftovers"]
    saved.leftovers_used.clear()
    saved.leftovers_used.update(snap["leftovers_used"])


def _commit(
    state: TrackingState, saved: SavedPlan, mutate: Callable[[], str | None]
) -> TrackingResult:
    snap = _snapshot(state, saved)
    try:
        message = mutate()
        state.persist(plan=saved, pantry=state.pantry, leftovers=state.prepared_leftovers)
        return TrackingResult(True, message)
    except Exception:  # noqa: BLE001 - any failure means full rollback
        logger.exception("meal tracking operation failed; rolled back")
        _restore(state, saved, snap)
        return TrackingResult(False, SAVE_FAILED_MESSAGE)


# -- prepare / eat ---------------------------------------------------------


def prepare_and_eat(
    state: TrackingState, saved: SavedPlan, when: date, slot: MealSlot, meal: Meal
) -> TrackingResult:
    """The single Eaten button: deduct ingredients (or consume the reserved
    leftover) once, and mark the meal eaten. Re-clicking after a display
    correction only restores the display — inventory is guarded by
    ``meal_was_prepared``."""
    entry = saved.tracking_entry(when, slot)
    if meal_was_prepared(entry):
        note = str(entry.get("leftover_note", ""))

        def restore_display() -> str | None:
            saved.set_tracking(when, slot, True, note)
            return None

        return _commit(state, saved, restore_display)

    leftover_id = _leftover_ref(entry, meal)

    def mutate() -> str | None:
        message: str | None = None
        if leftover_id:
            reserved, consumed = eat_prepared_leftover(
                saved, state.leftovers_by_id, state.foods_by_id, when, slot, leftover_id
            )
            if consumed + EPSILON < reserved:
                message = STALE_LEFTOVER_MESSAGE
        else:
            mark_ingredients_used(saved, state.pantry, when, slot, meal, 1.0)
            if meal.batch_id and not meal.is_leftover:
                _create_batch_leftover(state, saved, when, slot, meal)
        saved.set_prepared(when, slot, True)
        saved.set_tracking(when, slot, True, "")
        return message

    return _commit(state, saved, mutate)


def _serving_portions(
    meal: Meal, remaining_fraction_of: Callable[[str], float] | None = None, only: str | None = None
) -> list[PreparedFoodPortion]:
    """Build ONE serving's leftover portions with component provenance.

    Portions are aggregated by food id (one per food — the eat/undo accounting
    keys on food id). Each carries its component_kind (``"both"`` when the same
    food appears in both the main and the side) and the recipe it came from, so
    a leftover records whether it holds the main, the side, or both.

    ``only`` restricts to a single component ("main" for a batch dinner's
    doubled second serving). ``remaining_fraction_of`` maps a food id to the
    fraction still on the plate (defaults to a full, untouched serving).
    """
    agg: dict[str, dict] = {}
    for portion in meal.portions:
        if only is not None and portion.component_kind != only:
            continue
        entry = agg.setdefault(
            portion.food.id,
            {"name": portion.food.name, "grams": 0.0, "kinds": set(), "recipe": None},
        )
        entry["grams"] += portion.grams
        entry["kinds"].add(portion.component_kind)
        # Prefer the main recipe id, fall back to whatever the portion carries.
        if entry["recipe"] is None or portion.component_kind == COMPONENT_MAIN:
            entry["recipe"] = portion.source_recipe_id or (
                meal.recipe_id if portion.component_kind == COMPONENT_MAIN else meal.side_recipe_id
            )
    portions: list[PreparedFoodPortion] = []
    for fid, entry in sorted(agg.items()):
        kinds = entry["kinds"]
        if COMPONENT_MAIN in kinds and COMPONENT_SIDE in kinds:
            kind = COMPONENT_BOTH
        else:
            kind = next(iter(kinds), COMPONENT_MAIN)
        grams = entry["grams"]
        fraction = 1.0 if remaining_fraction_of is None else remaining_fraction_of(fid)
        portions.append(
            PreparedFoodPortion(
                food_id=fid,
                food_name=entry["name"],
                original_grams=grams,
                remaining_grams=grams * fraction,
                component_kind=kind,
                source_recipe_id=entry["recipe"],
            )
        )
    return portions


def _create_batch_leftover(
    state: TrackingState, saved: SavedPlan, when: date, slot: MealSlot, meal: Meal
) -> None:
    """Cooking a batch dinner physically creates tomorrow's serving — record it
    in the global store so it survives plan regeneration.

    The link to tomorrow's lunch is only written when that meal verifiably IS
    this batch's leftover (same batch_id, still a plain batch leftover, not
    eaten). Otherwise the record enters the store unreserved and waits for the
    next plan's pre-pass. If the lunch was already marked eaten, the second
    serving is gone — no record at all.
    """
    next_day = when + timedelta(days=1)
    lunch_day = saved.day_for_date(next_day)
    lunch_meal = lunch_day.meal_for(MealSlot.LUNCH) if lunch_day is not None else None
    lunch_entry = saved.tracking_entry(next_day, MealSlot.LUNCH)
    lunch_matches = (
        lunch_meal is not None
        and lunch_meal.is_leftover
        and lunch_meal.batch_id == meal.batch_id
        and lunch_meal.prepared_leftover_id is None
        and lunch_entry.get("linked_leftover_id") is None
    )
    if lunch_matches and (bool(lunch_entry.get("eaten")) or meal_was_prepared(lunch_entry)):
        return  # the second serving was already eaten (out-of-order tracking)

    # Only the MAIN is batch-cooked twice (meal_draw_grams doubles main portions
    # only), so the second serving that becomes tomorrow's lunch holds the main
    # component. A main-less meal (shouldn't happen for a batch) falls back to
    # the whole serving.
    batch_portions = _serving_portions(meal, only=COMPONENT_MAIN) or _serving_portions(meal)
    leftover = PreparedLeftover(
        id=uuid.uuid4().hex,
        origin_kind=ORIGIN_BATCH,
        source_date=when.isoformat(),
        source_slot=slot.value,
        source_meal_template_id=meal.template_id,
        meal_name=meal.name,
        note="",
        initial_fraction_remaining=1.0,
        servings_remaining=0.0,  # derived below
        portions=batch_portions,
        prepared_at=when.isoformat(),
        use_by_date=suggested_use_by(when),
        created_at=datetime.now().isoformat(timespec="seconds"),
        status=STATUS_AVAILABLE,
    )
    refresh_derived_fields(leftover, state.foods_by_id)
    state.prepared_leftovers.append(leftover)
    saved.set_leftover_link(when, slot, "batch_leftover_id", leftover.id)
    if lunch_matches:
        saved.set_leftover_link(next_day, MealSlot.LUNCH, "linked_leftover_id", leftover.id)
        saved.leftovers_used[leftover.id] = 1.0


# -- reported leftovers ----------------------------------------------------


def can_edit_leftover(state: TrackingState, saved: SavedPlan, when: date, slot: MealSlot) -> bool:
    """Whether "Edit amount eaten" may rewrite the created leftover record.
    False once the record has downstream history (partially eaten, discarded,
    or reserved by the plan) — then only the display note may change."""
    entry = saved.tracking_entry(when, slot)
    leftover_id = entry.get("leftover_created_id")
    if leftover_id is None:
        return True  # nothing recorded yet — first-time input is always fine
    leftover = state.leftovers_by_id.get(leftover_id)
    if leftover is None:
        return False
    return (
        leftover.status == STATUS_AVAILABLE
        and abs(leftover.servings_remaining - leftover.initial_fraction_remaining) <= EPSILON
        and leftover_id not in saved.leftovers_used
    )


def record_leftover(
    state: TrackingState,
    saved: SavedPlan,
    when: date,
    slot: MealSlot,
    meal: Meal,
    overall_fraction: float,
    components: dict[str, float],
    note: str,
) -> TrackingResult:
    """Record how much of this serving is left as a PreparedLeftover.

    The pantry is never touched — cooked food cannot become raw stock again.
    Components override the overall fraction per food id; the record's
    servings are derived from the component grams (single source of truth).
    Editing an existing record replaces it in place (no duplicates); a zero
    result deletes it.
    """
    entry = saved.tracking_entry(when, slot)
    if meal.is_leftover or meal.prepared_leftover_id is not None:
        return TrackingResult(False, "Adjust this leftover from the Pantry page instead.")
    if not meal_was_prepared(entry):
        return TrackingResult(False, "Mark the meal eaten first.")
    if not can_edit_leftover(state, saved, when, slot):
        return TrackingResult(False, "This leftover already has history — only the note can change.")

    def mutate() -> str | None:
        overall = min(max(float(overall_fraction), 0.0), 1.0)
        portions = _serving_portions(
            meal,
            remaining_fraction_of=lambda fid: min(max(components.get(fid, overall), 0.0), 1.0),
        )
        derived = derive_remaining_fraction(portions, state.foods_by_id)
        existing_id = entry.get("leftover_created_id")
        existing = state.leftovers_by_id.get(existing_id) if existing_id else None

        if derived <= EPSILON:  # fully eaten — no leftover record
            if existing is not None:
                state.prepared_leftovers.remove(existing)
            saved.set_leftover_link(when, slot, "leftover_created_id", None)
            saved.set_tracking(when, slot, True, note)
            return None

        # The AI's overall value is display-only; when it contradicts its own
        # components, the derived value wins there too.
        display_fraction = (
            overall if abs(overall - derived) <= OVERALL_MISMATCH_TOLERANCE else derived
        )
        if existing is not None:
            existing.portions = portions
            existing.initial_fraction_remaining = display_fraction
            existing.note = note
            refresh_derived_fields(existing, state.foods_by_id)
        else:
            leftover = PreparedLeftover(
                id=uuid.uuid4().hex,
                origin_kind=ORIGIN_USER,
                source_date=when.isoformat(),
                source_slot=slot.value,
                source_meal_template_id=meal.template_id,
                meal_name=meal.name,
                note=note,
                initial_fraction_remaining=display_fraction,
                servings_remaining=0.0,  # derived below
                portions=portions,
                prepared_at=when.isoformat(),
                use_by_date=suggested_use_by(when),
                created_at=datetime.now().isoformat(timespec="seconds"),
                status=STATUS_AVAILABLE,
            )
            refresh_derived_fields(leftover, state.foods_by_id)
            state.prepared_leftovers.append(leftover)
            saved.set_leftover_link(when, slot, "leftover_created_id", leftover.id)
        saved.set_tracking(when, slot, True, note)
        return None

    return _commit(state, saved, mutate)


def correct_leftover_note(
    state: TrackingState, saved: SavedPlan, when: date, slot: MealSlot, note: str
) -> TrackingResult:
    """"Correct original note": fix the displayed note without touching any
    inventory — for records that already have downstream history."""

    def mutate() -> str | None:
        entry = saved.tracking_entry(when, slot)
        saved.set_tracking(when, slot, bool(entry.get("eaten")), note)
        leftover_id = entry.get("leftover_created_id")
        leftover = state.leftovers_by_id.get(leftover_id) if leftover_id else None
        if leftover is not None:
            leftover.note = note
        return None

    return _commit(state, saved, mutate)


# -- undo ------------------------------------------------------------------


def _untouched_since_creation(leftover: PreparedLeftover) -> bool:
    return (
        leftover.status == STATUS_AVAILABLE
        and abs(leftover.servings_remaining - leftover.initial_fraction_remaining) <= EPSILON
    )


def _untouched_since_consumption(entry: dict, leftover: PreparedLeftover) -> bool:
    """The record still holds exactly before − consumed for every food, so
    adding the consumed grams back reproduces the pre-consumption state."""
    consumed = entry.get("leftover_consumed_grams") or {}
    before = entry.get("leftover_before_grams") or {}
    for portion in leftover.portions:
        expected = before.get(portion.food_id, portion.original_grams) - consumed.get(
            portion.food_id, 0.0
        )
        if abs(portion.remaining_grams - expected) > 1e-3:
            return False
    return True


def can_undo_preparation(
    state: TrackingState, saved: SavedPlan, when: date, slot: MealSlot, meal: Meal
) -> bool:
    """Undo preparation restores raw ingredients / consumed servings — only
    safe while nothing downstream happened to the records it must delete or
    refill. Otherwise the UI offers "Correct display status" instead."""
    entry = saved.tracking_entry(when, slot)
    if not meal_was_prepared(entry):
        return False

    leftover_id = _leftover_ref(entry, meal)
    if leftover_id:  # leftover-backed meal: undo refills the consumed record
        if entry.get("leftover_consumed") is None:
            return False
        if float(entry.get("leftover_consumed") or 0.0) <= EPSILON:
            return True  # stale consumption recorded nothing; undo just clears it
        leftover = state.leftovers_by_id.get(leftover_id)
        return (
            leftover is not None
            and leftover.status in (STATUS_AVAILABLE, STATUS_CONSUMED)
            and _untouched_since_consumption(entry, leftover)
        )

    created_id = entry.get("leftover_created_id")
    if created_id:
        created = state.leftovers_by_id.get(created_id)
        if created is not None and not (
            _untouched_since_creation(created) and created_id not in saved.leftovers_used
        ):
            return False
    batch_id = entry.get("batch_leftover_id")
    if batch_id:
        batch = state.leftovers_by_id.get(batch_id)
        if batch is not None and not _untouched_since_creation(batch):
            return False
        # our own reservation for tomorrow's lunch is fine — undo clears it —
        # but the lunch itself must not have been eaten yet
        lunch_entry = saved.tracking_entry(when + timedelta(days=1), MealSlot.LUNCH)
        if bool(lunch_entry.get("eaten")) or lunch_entry.get("leftover_consumed") is not None:
            return False
    return True


def undo_preparation(
    state: TrackingState, saved: SavedPlan, when: date, slot: MealSlot, meal: Meal
) -> TrackingResult:
    """The meal was never actually cooked: restore raw ingredients, delete the
    records its preparation created, and clear the tracking state. Callers
    must gate on can_undo_preparation."""
    if not can_undo_preparation(state, saved, when, slot, meal):
        return TrackingResult(False, "This meal's leftovers were already used — undo isn't safe.")
    entry = saved.tracking_entry(when, slot)
    leftover_id = _leftover_ref(entry, meal)

    def mutate() -> str | None:
        if leftover_id:
            undo_prepared_leftover(
                saved, state.leftovers_by_id, state.foods_by_id, when, slot, leftover_id
            )
        else:
            undo_ingredients_used(saved, state.pantry, when, slot)
            batch_record_id = entry.get("batch_leftover_id")
            for key in ("leftover_created_id", "batch_leftover_id"):
                linked_id = entry.get(key)
                linked = state.leftovers_by_id.get(linked_id) if linked_id else None
                if linked is not None:
                    state.prepared_leftovers.remove(linked)
                if linked_id:
                    saved.leftovers_used.pop(linked_id, None)
                    saved.set_leftover_link(when, slot, key, None)
            if batch_record_id:
                next_day = when + timedelta(days=1)
                lunch_entry = saved.tracking.get(next_day.isoformat(), {}).get(
                    MealSlot.LUNCH.value
                )
                if lunch_entry is not None and (
                    lunch_entry.get("linked_leftover_id") == batch_record_id
                ):
                    saved.set_leftover_link(next_day, MealSlot.LUNCH, "linked_leftover_id", None)
        saved.set_prepared(when, slot, False)
        saved.set_tracking(when, slot, False, "")
        return None

    return _commit(state, saved, mutate)


def correct_display_status(
    state: TrackingState, saved: SavedPlan, when: date, slot: MealSlot
) -> TrackingResult:
    """"Correct display status": clear the eaten flag and note WITHOUT touching
    pantry or leftovers. ``prepared`` stays set, so a later Eaten click only
    restores the display and can never deduct twice."""

    def mutate() -> str | None:
        saved.set_tracking(when, slot, False, "")
        return None

    return _commit(state, saved, mutate)


# -- reservations ----------------------------------------------------------


def reserved_slot_for(saved: SavedPlan | None, leftover_id: str) -> tuple[date, MealSlot] | None:
    """Where the current plan reserves this leftover, if anywhere: either a
    pre-pass-scheduled ready meal or a batch dinner's linked next-day lunch."""
    if saved is None or leftover_id not in saved.leftovers_used:
        return None
    for day in saved.meal_plan.days:
        for meal in day.meals:
            if meal.prepared_leftover_id == leftover_id:
                return saved.start_date + timedelta(days=day.day_index), meal.slot
    for date_iso, slots in saved.tracking.items():
        for slot_value, entry in slots.items():
            if entry.get("linked_leftover_id") == leftover_id:
                try:
                    return date.fromisoformat(date_iso), MealSlot(slot_value)
                except ValueError:
                    continue
    # Reserved but not locatable (shouldn't happen) — still report reserved.
    return saved.start_date, MealSlot.LUNCH
