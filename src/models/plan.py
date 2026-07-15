"""Saved plan model: the last built plan, persisted locally for the calendar.

Portions serialize as (food_id, grams) and are rehydrated against the seed
food catalog on load; nutrients are recomputed, never stored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from models.basket import BudgetStatus, NutrientGap
from models.explanation import Explanation
from models.food import Food, Nutrients
from models.meals import (
    DayPlan, Meal, MealPlan, MealPortion, MealSlot, SOURCE_LEGACY, SOURCE_RECIPE,
)

PLAN_SCHEMA_VERSION = 5
_ACCEPTED_VERSIONS = (2, 3, 4, PLAN_SCHEMA_VERSION)

# Deterministic namespace for ids derived from legacy data (v2 plans without
# a plan_id, synthetic purchase records): the same input always maps to the
# same id, so an interrupted migration can safely retry.
RIGHTMEAL_NS = uuid.uuid5(uuid.NAMESPACE_URL, "rightmeal.local")


def new_plan_id() -> str:
    return str(uuid.uuid4())


def legacy_plan_id(created_at: str, start_date: str, horizon_days: int, budget: float) -> str:
    return str(uuid.uuid5(RIGHTMEAL_NS, f"{created_at}|{start_date}|{horizon_days}|{budget}"))


def _default_tracking_entry() -> dict:
    return {
        "eaten": False,
        "leftover_note": "",
        "used_fraction": None,
        "pantry_deducted": {},
        # None on all of these means "not set" (legacy entries load as None).
        "prepared": None,  # ingredients were deducted for this meal (vs. eaten = display)
        "leftover_consumed": None,  # servings consumed from a prepared leftover
        "leftover_consumed_grams": None,  # food_id -> raw-equivalent grams actually consumed
        "leftover_before_grams": None,  # food_id -> remaining_grams snapshot before consuming
        "leftover_created_id": None,  # PreparedLeftover this meal's reported leftovers created
        "batch_leftover_id": None,  # batch second serving auto-created by this dinner
        "linked_leftover_id": None,  # leftover this (batch lunch) meal consumes when eaten
    }


@dataclass(frozen=True)
class SavedBasketItem:
    """A basket line frozen at plan time (quote details flattened for display)."""

    food_id: str
    package_label: str
    count: int
    cost: float
    source: str  # PriceSource value
    store: str
    confidence: float
    match_reason: str
    matched_product_name: str


@dataclass(frozen=True)
class SavedUnusedFood:
    """A catalog food that didn't make the basket (category is an UnusedCategory value)."""

    category: str
    food_id: str
    reason: str


@dataclass
class SavedPlan:
    start_date: date
    horizon_days: int
    created_at: str  # ISO 8601
    budget: float
    total_cost: float
    meal_plan: MealPlan
    basket: tuple[SavedBasketItem, ...]
    consumed_gaps: tuple[NutrientGap, ...]
    # Stable identity — purchase records key on this, never on created_at.
    # Legacy v2 plans get a deterministic uuid5 on load (same plan -> same id).
    plan_id: str = field(default_factory=new_plan_id)
    # True when loading migrated old data (v2 schema / missing plan_id): the
    # app should persist once at startup so the migration sticks. Not stored.
    needs_resave: bool = field(default=False, compare=False)
    # date ISO -> slot value -> tracking entry (see _default_tracking_entry —
    # the four original keys plus optional leftover/preparation keys that
    # legacy plans load as None).
    tracking: dict[str, dict[str, dict]] = field(default_factory=dict)
    # food_id -> grams added to the pantry when the user checked "Purchased"
    # (presence = checked; the recorded grams make unchecking exact).
    purchased: dict[str, float] = field(default_factory=dict)
    # food_id -> pantry grams held BEFORE the purchase was checked off. Undo is
    # only safe while stock-after-undo would not dip below this waterline.
    purchased_baseline: dict[str, float] = field(default_factory=dict)
    # food_id -> pantry grams this plan counts on using (optimizer seed output).
    pantry_used: dict[str, float] = field(default_factory=dict)
    # leftover_id -> household servings this plan reserves (a reservation only:
    # the leftovers store is decremented when the meal is actually eaten).
    leftovers_used: dict[str, float] = field(default_factory=dict)
    # Everything the Plan page needs to render without re-running the pipeline.
    purchased_totals: Nutrients = field(default_factory=Nutrients)
    explanation: Explanation | None = None
    nutrition_feasible: bool = True
    budget_status: BudgetStatus = BudgetStatus.UNKNOWN
    relaxed_constraints: tuple[str, ...] = ()
    dominance_flags: tuple[str, ...] = ()
    unused: tuple[SavedUnusedFood, ...] = ()
    # Recipe-first plan-level metadata (v4). variety_mode is the mode the plan
    # was generated with; staples is the deduped low-quantity seasoning names to
    # check off (never priced, never a core ingredient).
    variety_mode: str = "balanced"
    staples: tuple[str, ...] = ()

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.horizon_days - 1)

    def day_for_date(self, when: date) -> DayPlan | None:
        offset = (when - self.start_date).days
        if 0 <= offset < len(self.meal_plan.days):
            return self.meal_plan.days[offset]
        return None

    def tracking_entry(self, when: date, slot: MealSlot) -> dict:
        return self.tracking.get(when.isoformat(), {}).get(
            slot.value, _default_tracking_entry()
        )

    def set_tracking(self, when: date, slot: MealSlot, eaten: bool, leftover_note: str) -> None:
        entry = self._entry_for_update(when, slot)
        entry["eaten"] = bool(eaten)
        entry["leftover_note"] = str(leftover_note)

    def set_ingredients_used(
        self, when: date, slot: MealSlot, fraction: float, deducted: dict[str, float]
    ) -> None:
        entry = self._entry_for_update(when, slot)
        entry["used_fraction"] = float(fraction)
        entry["pantry_deducted"] = {str(fid): float(g) for fid, g in deducted.items()}

    def clear_ingredients_used(self, when: date, slot: MealSlot) -> None:
        entry = self.tracking.get(when.isoformat(), {}).get(slot.value)
        if entry is None:
            return
        entry["used_fraction"] = None
        entry["pantry_deducted"] = {}

    def set_prepared(self, when: date, slot: MealSlot, prepared: bool) -> None:
        self._entry_for_update(when, slot)["prepared"] = bool(prepared)

    def set_leftover_consumption(
        self,
        when: date,
        slot: MealSlot,
        servings: float | None,
        consumed_grams: dict[str, float] | None,
        before_grams: dict[str, float] | None,
    ) -> None:
        """Record (or clear, with all None) what eating a prepared-leftover meal
        actually consumed — the exact data undo needs."""
        entry = self._entry_for_update(when, slot)
        entry["leftover_consumed"] = float(servings) if servings is not None else None
        entry["leftover_consumed_grams"] = (
            {str(fid): float(g) for fid, g in consumed_grams.items()}
            if consumed_grams is not None
            else None
        )
        entry["leftover_before_grams"] = (
            {str(fid): float(g) for fid, g in before_grams.items()}
            if before_grams is not None
            else None
        )

    def set_leftover_link(
        self, when: date, slot: MealSlot, key: str, leftover_id: str | None
    ) -> None:
        if key not in ("leftover_created_id", "batch_leftover_id", "linked_leftover_id"):
            raise ValueError(f"not a leftover link key: {key}")
        self._entry_for_update(when, slot)[key] = (
            str(leftover_id) if leftover_id is not None else None
        )

    def _entry_for_update(self, when: date, slot: MealSlot) -> dict:
        """The stored (mutable) entry for a slot, created with defaults if absent —
        updates to one field never clobber the others."""
        slots = self.tracking.setdefault(when.isoformat(), {})
        return slots.setdefault(slot.value, _default_tracking_entry())

    def to_dict(self) -> dict:
        return {
            "version": PLAN_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "start_date": self.start_date.isoformat(),
            "horizon_days": self.horizon_days,
            "created_at": self.created_at,
            "budget": self.budget,
            "total_cost": self.total_cost,
            "basket": [
                {
                    "food_id": item.food_id,
                    "package_label": item.package_label,
                    "count": item.count,
                    "cost": item.cost,
                    "source": item.source,
                    "store": item.store,
                    "confidence": item.confidence,
                    "match_reason": item.match_reason,
                    "matched_product_name": item.matched_product_name,
                }
                for item in self.basket
            ],
            "meal_plan": {
                "horizon_days": self.meal_plan.horizon_days,
                "pantry_carryover": {
                    fid: round(grams, 3) for fid, grams in sorted(self.meal_plan.pantry_carryover.items())
                },
                "days": [
                    {
                        "day_index": day.day_index,
                        "meals": [
                            {
                                "slot": meal.slot.value,
                                "template_id": meal.template_id,
                                "recipe_id": meal.recipe_id,
                                "source_kind": meal.source_kind,
                                "servings": meal.servings,
                                "side_recipe_id": meal.side_recipe_id,
                                "side_servings": meal.side_servings,
                                "name": meal.name,
                                "is_leftover": meal.is_leftover,
                                "batch_id": meal.batch_id,
                                "prepared_leftover_id": meal.prepared_leftover_id,
                                "portions": [
                                    {
                                        "food_id": p.food.id,
                                        "grams": round(p.grams, 3),
                                        "cooked_grams": (
                                            round(p.cooked_grams, 3) if p.cooked_grams is not None else None
                                        ),
                                        "source_recipe_id": p.source_recipe_id,
                                        "component_kind": p.component_kind,
                                    }
                                    for p in meal.portions
                                ],
                            }
                            for meal in day.meals
                        ],
                    }
                    for day in self.meal_plan.days
                ],
            },
            "consumed_gaps": [
                {"nutrient": g.nutrient, "achieved": g.achieved, "target": g.target}
                for g in self.consumed_gaps
            ],
            "tracking": self.tracking,
            "purchased": {
                fid: round(grams, 3) for fid, grams in sorted(self.purchased.items())
            },
            "purchased_baseline": {
                fid: round(grams, 3) for fid, grams in sorted(self.purchased_baseline.items())
            },
            "pantry_used": {
                fid: round(grams, 3) for fid, grams in sorted(self.pantry_used.items())
            },
            "leftovers_used": {
                lid: round(servings, 4) for lid, servings in sorted(self.leftovers_used.items())
            },
            "purchased_totals": {
                name: round(value, 3) for name, value in self.purchased_totals.as_dict().items()
            },
            "explanation": (
                {
                    "summary": self.explanation.summary,
                    "item_reasons": dict(self.explanation.item_reasons),
                    "nutrition_gaps": list(self.explanation.nutrition_gaps),
                    "budget_tradeoffs": self.explanation.budget_tradeoffs,
                    "food_group_coverage": self.explanation.food_group_coverage,
                    "life_impact": self.explanation.life_impact,
                    "generated_by": self.explanation.generated_by,
                }
                if self.explanation is not None
                else None
            ),
            "feasibility": {
                "nutrition_feasible": self.nutrition_feasible,
                "budget_status": self.budget_status.value,
                "relaxed_constraints": list(self.relaxed_constraints),
                "dominance_flags": list(self.dominance_flags),
            },
            "unused": [
                {"category": u.category, "food_id": u.food_id, "reason": u.reason}
                for u in self.unused
            ],
            "variety_mode": self.variety_mode,
            "staples": list(self.staples),
        }

    @classmethod
    def from_dict(cls, data: dict, foods_by_id: dict[str, Food]) -> "SavedPlan | None":
        version = data.get("version")
        if version not in _ACCEPTED_VERSIONS:
            return None
        # v2 -> v3 migration: derive a DETERMINISTIC plan_id, so the same
        # legacy plan maps to the same id even across interrupted saves.
        # v2/v3 -> v4: template meals become legacy meals (recipe_id=None,
        # source_kind="legacy_template"); a resave persists the upgrade.
        plan_id = str(data.get("plan_id") or "")
        needs_resave = version < PLAN_SCHEMA_VERSION or not plan_id
        if not plan_id:
            plan_id = legacy_plan_id(
                str(data.get("created_at", "")),
                str(data.get("start_date", "")),
                int(data.get("horizon_days", 0)),
                float(data.get("budget", 0.0)),
            )
        raw_plan = data["meal_plan"]
        days: list[DayPlan] = []
        consumed = Nutrients()
        for raw_day in raw_plan["days"]:
            meals: list[Meal] = []
            for raw_meal in raw_day["meals"]:
                portions: list[MealPortion] = []
                for raw_portion in raw_meal["portions"]:
                    food = foods_by_id.get(str(raw_portion["food_id"]))
                    if food is None:
                        return None
                    portions.append(
                        MealPortion(
                            food=food,
                            grams=float(raw_portion["grams"]),
                            cooked_grams=(
                                float(raw_portion["cooked_grams"])
                                if raw_portion.get("cooked_grams") is not None
                                else None
                            ),
                            source_recipe_id=raw_portion.get("source_recipe_id"),
                            component_kind=str(raw_portion.get("component_kind", "main")),
                        )
                    )
                raw_leftover_id = raw_meal.get("prepared_leftover_id")
                raw_recipe_id = raw_meal.get("recipe_id")
                # v2/v3 meals had only template_id; they become legacy meals.
                default_source = SOURCE_RECIPE if raw_recipe_id else SOURCE_LEGACY
                meal = Meal(
                    slot=MealSlot(raw_meal["slot"]),
                    template_id=str(raw_meal.get("template_id", "")),
                    name=str(raw_meal["name"]),
                    portions=tuple(portions),
                    recipe_id=str(raw_recipe_id) if raw_recipe_id else None,
                    source_kind=str(raw_meal.get("source_kind", default_source)),
                    servings=float(raw_meal.get("servings", 0.0)),
                    side_recipe_id=(
                        str(raw_meal["side_recipe_id"]) if raw_meal.get("side_recipe_id") else None
                    ),
                    side_servings=float(raw_meal.get("side_servings", 0.0)),
                    is_leftover=bool(raw_meal.get("is_leftover", False)),
                    batch_id=raw_meal.get("batch_id"),
                    prepared_leftover_id=(
                        str(raw_leftover_id) if raw_leftover_id is not None else None
                    ),
                )
                meals.append(meal)
                consumed = consumed.plus(meal.nutrients)
            days.append(DayPlan(day_index=int(raw_day["day_index"]), meals=tuple(meals)))
        carryover: dict[str, float] = {}
        for fid, grams in raw_plan.get("pantry_carryover", {}).items():
            if fid not in foods_by_id:
                return None
            carryover[str(fid)] = float(grams)
        basket = tuple(
            SavedBasketItem(
                food_id=str(raw["food_id"]),
                package_label=str(raw["package_label"]),
                count=int(raw["count"]),
                cost=float(raw["cost"]),
                source=str(raw["source"]),
                store=str(raw["store"]),
                confidence=float(raw["confidence"]),
                match_reason=str(raw["match_reason"]),
                matched_product_name=str(raw["matched_product_name"]),
            )
            for raw in data.get("basket", [])
        )
        if any(item.food_id not in foods_by_id for item in basket):
            return None
        def _optional_grams(entry: dict, key: str) -> dict[str, float] | None:
            value = entry.get(key)
            if value is None:
                return None
            return {str(fid): float(grams) for fid, grams in dict(value).items()}

        def _optional_id(entry: dict, key: str) -> str | None:
            value = entry.get(key)
            return str(value) if value is not None else None

        tracking = {
            str(date_iso): {
                str(slot): {
                    "eaten": bool(entry.get("eaten", False)),
                    "leftover_note": str(entry.get("leftover_note", "")),
                    "used_fraction": (
                        float(entry["used_fraction"])
                        if entry.get("used_fraction") is not None
                        else None
                    ),
                    "pantry_deducted": {
                        str(fid): float(grams)
                        for fid, grams in dict(entry.get("pantry_deducted", {})).items()
                    },
                    # Optional keys: legacy entries simply load as None.
                    "prepared": (
                        bool(entry["prepared"]) if entry.get("prepared") is not None else None
                    ),
                    "leftover_consumed": (
                        float(entry["leftover_consumed"])
                        if entry.get("leftover_consumed") is not None
                        else None
                    ),
                    "leftover_consumed_grams": _optional_grams(entry, "leftover_consumed_grams"),
                    "leftover_before_grams": _optional_grams(entry, "leftover_before_grams"),
                    "leftover_created_id": _optional_id(entry, "leftover_created_id"),
                    "batch_leftover_id": _optional_id(entry, "batch_leftover_id"),
                    "linked_leftover_id": _optional_id(entry, "linked_leftover_id"),
                }
                for slot, entry in slots.items()
            }
            for date_iso, slots in dict(data.get("tracking", {})).items()
        }
        # Purchase/pantry records are informational: drop unknown ids, don't fail.
        purchased = {
            str(fid): float(grams)
            for fid, grams in dict(data.get("purchased", {})).items()
            if str(fid) in foods_by_id
        }
        purchased_baseline = {
            str(fid): float(grams)
            for fid, grams in dict(data.get("purchased_baseline", {})).items()
            if str(fid) in foods_by_id
        }
        pantry_used = {
            str(fid): float(grams)
            for fid, grams in dict(data.get("pantry_used", {})).items()
            if str(fid) in foods_by_id
        }
        # Leftover ids are not food ids; staleness is resolved at eaten-time.
        leftovers_used = {
            str(lid): float(servings)
            for lid, servings in dict(data.get("leftovers_used", {})).items()
        }
        raw_explanation = data.get("explanation")
        explanation = (
            Explanation(
                summary=str(raw_explanation.get("summary", "")),
                item_reasons={
                    str(k): str(v) for k, v in dict(raw_explanation.get("item_reasons", {})).items()
                },
                nutrition_gaps=[str(x) for x in raw_explanation.get("nutrition_gaps", [])],
                budget_tradeoffs=str(raw_explanation.get("budget_tradeoffs", "")),
                food_group_coverage=str(raw_explanation.get("food_group_coverage", "")),
                life_impact=str(raw_explanation.get("life_impact", "")),
                generated_by=(
                    "openai" if raw_explanation.get("generated_by") == "openai" else "local"
                ),
            )
            if raw_explanation
            else None
        )
        feasibility = dict(data.get("feasibility", {}))
        # Precise migration of the old boolean: old True can't be trusted (old
        # code silently skipped unpriced items), old False can be — but only
        # when the basket wasn't empty (the old formula was
        # ``bool(items) and total_cost <= budget + 1e-6``).
        raw_status = feasibility.get("budget_status")
        if raw_status in {s.value for s in BudgetStatus}:
            budget_status = BudgetStatus(raw_status)
        elif feasibility.get("budget_feasible") is False and data.get("basket"):
            budget_status = BudgetStatus.OVER
        else:
            budget_status = BudgetStatus.UNKNOWN
        # Unused foods are informational: drop entries for unknown ids instead of failing.
        unused = tuple(
            SavedUnusedFood(
                category=str(raw["category"]),
                food_id=str(raw["food_id"]),
                reason=str(raw["reason"]),
            )
            for raw in data.get("unused", [])
            if str(raw.get("food_id", "")) in foods_by_id
        )
        return cls(
            plan_id=plan_id,
            needs_resave=needs_resave,
            start_date=date.fromisoformat(str(data["start_date"])),
            horizon_days=int(data["horizon_days"]),
            created_at=str(data.get("created_at", "")),
            budget=float(data["budget"]),
            total_cost=float(data["total_cost"]),
            meal_plan=MealPlan(
                days=tuple(days),
                pantry_carryover=carryover,
                consumed_totals=consumed,
                horizon_days=int(raw_plan["horizon_days"]),
            ),
            basket=basket,
            consumed_gaps=tuple(
                NutrientGap(
                    nutrient=str(raw["nutrient"]),
                    achieved=float(raw["achieved"]),
                    target=float(raw["target"]),
                )
                for raw in data.get("consumed_gaps", [])
            ),
            tracking=tracking,
            purchased=purchased,
            purchased_baseline=purchased_baseline,
            pantry_used=pantry_used,
            leftovers_used=leftovers_used,
            purchased_totals=Nutrients.from_dict(dict(data.get("purchased_totals", {}))),
            explanation=explanation,
            nutrition_feasible=bool(feasibility.get("nutrition_feasible", True)),
            budget_status=budget_status,
            relaxed_constraints=tuple(
                str(x) for x in feasibility.get("relaxed_constraints", [])
            ),
            dominance_flags=tuple(str(x) for x in feasibility.get("dominance_flags", [])),
            unused=unused,
            variety_mode=str(data.get("variety_mode", "balanced")),
            staples=tuple(str(s) for s in data.get("staples", [])),
        )
