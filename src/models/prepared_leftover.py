"""Prepared leftovers: cooked meals kept as ready-to-eat servings.

Cooking is one-way — a leftover is never raw pantry stock and its portions
must never be written back into the pantry. Each portion keeps the
raw-equivalent grams that went into one household serving of the source meal,
so nutrition stays honest per ingredient ("the rice is half left but the
chicken is gone").

``remaining_grams`` per portion is the single source of truth for how much of
the dish exists. ``servings_remaining`` is a derived cache: every code path
that mutates portions must finish with ``refresh_derived_fields`` and loading
never trusts the serialized cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from models.food import Food

LEFTOVERS_SCHEMA_VERSION = 2

SUGGESTED_USE_BY_DAYS = 3  # advisory, not a food-safety guarantee

# Which recipe component a leftover portion came from. A dish can leave over its
# main only, its side only, or both — component provenance is preserved so
# reheating/replenishment know which part they are dealing with.
COMPONENT_MAIN = "main"
COMPONENT_SIDE = "side"
COMPONENT_BOTH = "both"  # an aggregated portion whose food appears in both

STATUS_AVAILABLE = "available"
STATUS_CONSUMED = "consumed"
STATUS_DISCARDED = "discarded"
_STATUSES = (STATUS_AVAILABLE, STATUS_CONSUMED, STATUS_DISCARDED)

ORIGIN_USER = "user"
ORIGIN_BATCH = "batch"

EPSILON = 1e-6


@dataclass
class PreparedFoodPortion:
    food_id: str
    food_name: str
    original_grams: float  # raw grams in ONE serving of the source meal (immutable)
    remaining_grams: float  # raw-equivalent grams still on the plate (mutable)
    # Component provenance (see COMPONENT_* above). Defaults keep every existing
    # 4-argument construction working and load v1 records as plain mains.
    component_kind: str = COMPONENT_MAIN  # "main" | "side" | "both"
    source_recipe_id: str | None = None


@dataclass
class PreparedLeftover:
    id: str
    origin_kind: str  # "user" (reported leftovers) | "batch" (auto second serving)
    source_date: str  # ISO date of the source meal
    source_slot: str
    source_meal_template_id: str
    meal_name: str
    note: str
    initial_fraction_remaining: float  # the user's original estimate, display only
    servings_remaining: float  # derived cache — see refresh_derived_fields
    portions: list[PreparedFoodPortion] = field(default_factory=list)
    prepared_at: str = ""  # ISO date the dish was actually cooked
    use_by_date: str = ""  # suggested (prepared_at + SUGGESTED_USE_BY_DAYS)
    created_at: str = ""  # when this record was created (not used for ordering)
    status: str = STATUS_AVAILABLE

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "origin_kind": self.origin_kind,
            "source_date": self.source_date,
            "source_slot": self.source_slot,
            "source_meal_template_id": self.source_meal_template_id,
            "meal_name": self.meal_name,
            "note": self.note,
            "initial_fraction_remaining": round(self.initial_fraction_remaining, 4),
            "servings_remaining": round(self.servings_remaining, 4),
            "portions": [
                {
                    "food_id": p.food_id,
                    "food_name": p.food_name,
                    "original_grams": round(p.original_grams, 3),
                    "remaining_grams": round(p.remaining_grams, 3),
                    "component_kind": p.component_kind,
                    "source_recipe_id": p.source_recipe_id,
                }
                for p in self.portions
            ],
            "prepared_at": self.prepared_at,
            "use_by_date": self.use_by_date,
            "created_at": self.created_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict, foods_by_id: dict[str, Food]) -> "PreparedLeftover | None":
        """Tolerant reader: a malformed record loads as None (dropped), never raises.

        The serialized ``servings_remaining`` is deliberately ignored — it is
        re-derived from the portions so a hand-edited or stale cache can never
        become a second source of inventory truth.
        """
        try:
            status = str(data.get("status", STATUS_AVAILABLE))
            if status not in _STATUSES:
                return None
            portions: list[PreparedFoodPortion] = []
            for raw in list(data.get("portions", [])):
                original = float(raw["original_grams"])
                remaining = float(raw["remaining_grams"])
                if original <= 0:
                    continue
                kind = str(raw.get("component_kind", COMPONENT_MAIN))
                if kind not in (COMPONENT_MAIN, COMPONENT_SIDE, COMPONENT_BOTH):
                    kind = COMPONENT_MAIN
                raw_recipe = raw.get("source_recipe_id")
                portions.append(
                    PreparedFoodPortion(
                        food_id=str(raw["food_id"]),
                        food_name=str(raw.get("food_name", "")),
                        original_grams=original,
                        remaining_grams=min(max(remaining, 0.0), original),
                        component_kind=kind,
                        source_recipe_id=(str(raw_recipe) if raw_recipe else None),
                    )
                )
            if not portions:
                return None
            initial = float(data.get("initial_fraction_remaining", 1.0))
            leftover = cls(
                id=str(data["id"]),
                origin_kind=str(data.get("origin_kind", ORIGIN_USER)),
                source_date=str(data.get("source_date", "")),
                source_slot=str(data.get("source_slot", "")),
                source_meal_template_id=str(data.get("source_meal_template_id", "")),
                meal_name=str(data.get("meal_name", "")),
                note=str(data.get("note", "")),
                initial_fraction_remaining=min(max(initial, 0.0), 1.0),
                servings_remaining=0.0,  # derived below
                portions=portions,
                prepared_at=str(data.get("prepared_at", "")),
                use_by_date=str(data.get("use_by_date", "")),
                created_at=str(data.get("created_at", "")),
                status=status,
            )
            refresh_derived_fields(leftover, foods_by_id)
            return leftover
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


def derive_remaining_fraction(
    portions: Iterable[PreparedFoodPortion], foods_by_id: dict[str, Food]
) -> float:
    """Fraction of the original serving still present, weighted by calories
    (grams-weighted when no portion has nutrition data)."""
    original_kcal = remaining_kcal = 0.0
    for p in portions:
        food = foods_by_id.get(p.food_id)
        if food is None:
            continue
        kcal_per_100 = food.nutrients_per_purchased_100g().calories_kcal
        original_kcal += kcal_per_100 * p.original_grams / 100.0
        remaining_kcal += kcal_per_100 * p.remaining_grams / 100.0
    if original_kcal > EPSILON:
        return max(remaining_kcal / original_kcal, 0.0)
    original_grams = sum(p.original_grams for p in portions)
    remaining_grams = sum(p.remaining_grams for p in portions)
    if original_grams <= EPSILON:
        return 0.0
    return max(remaining_grams / original_grams, 0.0)


def refresh_derived_fields(leftover: PreparedLeftover, foods_by_id: dict[str, Food]) -> None:
    """Re-establish the invariants after any portion mutation: grams clamped to
    [0, original], servings re-derived, and an emptied available record flips
    to consumed. Callers flip consumed→available explicitly (undo)."""
    for p in leftover.portions:
        if p.remaining_grams < EPSILON:
            p.remaining_grams = 0.0
        if p.remaining_grams > p.original_grams:
            p.remaining_grams = p.original_grams
    leftover.servings_remaining = derive_remaining_fraction(leftover.portions, foods_by_id)
    if leftover.status == STATUS_AVAILABLE and leftover.servings_remaining <= EPSILON:
        leftover.status = STATUS_CONSUMED


def remaining_grams_map(leftover: PreparedLeftover) -> dict[str, float]:
    """Current raw-equivalent grams per food id (aggregated)."""
    grams: dict[str, float] = {}
    for p in leftover.portions:
        grams[p.food_id] = grams.get(p.food_id, 0.0) + p.remaining_grams
    return grams


def component_summary(leftover: PreparedLeftover) -> str:
    """Whether this leftover holds the main only, the side only, or both —
    derived from the portions that still have food on the plate (falling back to
    all portions for a fully-eaten record). Returns "main", "side", "both", or
    "" when there is nothing to describe."""
    live = [p for p in leftover.portions if p.remaining_grams > EPSILON] or leftover.portions
    kinds: set[str] = set()
    for p in live:
        if p.component_kind == COMPONENT_BOTH:
            kinds.update({COMPONENT_MAIN, COMPONENT_SIDE})
        else:
            kinds.add(p.component_kind)
    if COMPONENT_MAIN in kinds and COMPONENT_SIDE in kinds:
        return COMPONENT_BOTH
    if COMPONENT_SIDE in kinds:
        return COMPONENT_SIDE
    if COMPONENT_MAIN in kinds:
        return COMPONENT_MAIN
    return ""


_COMPONENT_LABELS = {
    COMPONENT_MAIN: "Main dish",
    COMPONENT_SIDE: "Side dish",
    COMPONENT_BOTH: "Main + side",
}


def component_summary_label(leftover: PreparedLeftover) -> str:
    """A short human label ("Main dish" / "Side dish" / "Main + side") for the
    Pantry leftover card, or "" when there is nothing to describe."""
    return _COMPONENT_LABELS.get(component_summary(leftover), "")


def suggested_use_by(prepared_at: date) -> str:
    return (prepared_at + timedelta(days=SUGGESTED_USE_BY_DAYS)).isoformat()
