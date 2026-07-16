"""Typed planning outcomes and data-quality evidence.

The bounded planner must describe what it found, not turn search exhaustion
into a proof of impossibility.  These small immutable models keep that contract
independent from UI wording and from the current recipe-first payload type.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypeAlias

from models.meals import MealPlan, SLOT_ORDER

if TYPE_CHECKING:
    from planner.partial_plan import DailyFoodCoverage


DEFAULT_BEAM_WIDTH = 32
DEFAULT_MAX_CANDIDATES_PER_SLOT = 24


@dataclass(frozen=True)
class SearchLimits:
    """Deterministic search configuration plus observed bounded-search counts."""

    beam_width: int = DEFAULT_BEAM_WIDTH
    max_candidates_per_slot: int = DEFAULT_MAX_CANDIDATES_PER_SLOT
    pruned_state_count: int = 0
    candidate_count: int = 0
    search_exhaustive: bool = False
    algorithm: str = "budget-aware-beam"

    def __post_init__(self) -> None:
        if self.beam_width <= 0:
            raise ValueError("beam_width must be positive")
        if self.max_candidates_per_slot <= 0:
            raise ValueError("max_candidates_per_slot must be positive")
        if self.pruned_state_count < 0 or self.candidate_count < 0:
            raise ValueError("search counts must be non-negative")
        if not self.algorithm.strip():
            raise ValueError("algorithm is required")

    def with_counts(self, *, pruned_state_count: int, candidate_count: int) -> "SearchLimits":
        return replace(
            self,
            pruned_state_count=pruned_state_count,
            candidate_count=candidate_count,
        )


@dataclass(frozen=True, kw_only=True)
class PlanningDataIssue:
    """Base evidence for otherwise-eligible recipes blocked by missing data."""

    affected_count: int = 1
    recipe_ids: tuple[str, ...] = ()
    food_ids: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if self.affected_count <= 0:
            raise ValueError("affected_count must be positive")


@dataclass(frozen=True, kw_only=True)
class CatalogDataIncomplete(PlanningDataIssue):
    """A required catalog mapping or positive serving weight is incomplete."""


@dataclass(frozen=True, kw_only=True)
class RequiredIngredientUnmapped(PlanningDataIssue):
    """A required, non-optional food ingredient has no catalog mapping."""


@dataclass(frozen=True, kw_only=True)
class RequiredPriceUnavailable(PlanningDataIssue):
    """A required purchase gap has no positive package offer."""


@dataclass(frozen=True, kw_only=True)
class PackageDataUnavailable(PlanningDataIssue):
    """A mapped required food has no usable positive-size package."""


DataIssue: TypeAlias = (
    CatalogDataIncomplete
    | RequiredIngredientUnmapped
    | RequiredPriceUnavailable
    | PackageDataUnavailable
)


@dataclass(frozen=True, kw_only=True)
class StandardPlanReady:
    candidate: object
    estimated_total_cents: int
    estimated_cap_cents: int
    search_limits: SearchLimits = SearchLimits()
    data_issues: tuple[DataIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.estimated_total_cents < 0 or self.estimated_cap_cents < 0:
            raise ValueError("estimated cents must be non-negative")
        if self.estimated_total_cents > self.estimated_cap_cents:
            raise ValueError("a standard plan must fit the estimated cap")


@dataclass(frozen=True, kw_only=True)
class BudgetChoiceRequired:
    """A complete candidate exists above cap; it is not a minimum-cost proof."""

    candidate: object
    estimated_total_cents: int
    estimated_cap_cents: int
    search_limits: SearchLimits = SearchLimits()
    data_issues: tuple[DataIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.estimated_cap_cents < 0 or self.estimated_total_cents <= self.estimated_cap_cents:
            raise ValueError("a budget-choice candidate must be above the estimated cap")


@dataclass(frozen=True, kw_only=True)
class NoPlanFoundWithinSearchLimits:
    search_limits: SearchLimits = SearchLimits()
    reason: str = ""
    data_issues: tuple[DataIssue, ...] = ()


@dataclass(frozen=True, kw_only=True)
class NoFeasiblePlanProven:
    """Mathematically proven infeasibility, never ordinary beam exhaustion."""

    search_limits: SearchLimits
    proof_certificate: str | None = None
    reason: str = ""
    data_issues: tuple[DataIssue, ...] = ()

    def __post_init__(self) -> None:
        if not self.search_limits.search_exhaustive and not (
            self.proof_certificate and self.proof_certificate.strip()
        ):
            raise ValueError(
                "NoFeasiblePlanProven requires exhaustive search or a verified certificate"
            )


@dataclass(frozen=True, kw_only=True)
class DataUnavailable:
    issues: tuple[DataIssue, ...]
    search_limits: SearchLimits = SearchLimits()
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.issues:
            raise ValueError("DataUnavailable requires at least one data issue")

    @property
    def data_issues(self) -> tuple[DataIssue, ...]:
        return self.issues


@dataclass(frozen=True, kw_only=True)
class PartialFoodCoverageCandidate:
    """A cap-fitting, explicitly incomplete candidate awaiting confirmation.

    ``candidate`` is the legacy orchestration payload (currently a
    ``RecipeFirstOutput``) so callers retain its basket and presentation
    metadata.  ``meal_plan`` and ``daily_coverage`` are explicit because the
    partial plan's per-day portions and evidence must never be inferred from
    that opaque payload or from one household-wide serving number.
    """

    candidate: object
    meal_plan: MealPlan
    daily_coverage: tuple["DailyFoodCoverage", ...]
    estimated_total_cents: int
    estimated_cap_cents: int
    household_member_count: int
    rolling_recipe_max_uses: int
    search_limits: SearchLimits = SearchLimits()
    data_issues: tuple[DataIssue, ...] = ()
    remaining_budget_cents: int | None = None
    next_increment_total_cents: int | None = None

    def __post_init__(self) -> None:
        if self.estimated_total_cents < 0 or self.estimated_cap_cents < 0:
            raise ValueError("estimated cents must be non-negative")
        if self.estimated_total_cents > self.estimated_cap_cents:
            raise ValueError("a partial candidate must fit the estimated cap")
        expected_remaining = self.estimated_cap_cents - self.estimated_total_cents
        if self.remaining_budget_cents is None:
            object.__setattr__(self, "remaining_budget_cents", expected_remaining)
        elif self.remaining_budget_cents != expected_remaining:
            raise ValueError("remaining budget must equal cap minus estimated total")
        if (
            self.next_increment_total_cents is not None
            and self.next_increment_total_cents <= self.estimated_cap_cents
        ):
            raise ValueError(
                "the next partial-plan increment diagnostic must exceed the cap"
            )
        if self.household_member_count <= 0:
            raise ValueError("partial coverage must retain a positive household")
        if self.rolling_recipe_max_uses not in (2, 3):
            raise ValueError("partial recipe repetition may relax only from two to three")
        if len(self.meal_plan.days) != self.meal_plan.horizon_days:
            raise ValueError("partial coverage must retain the complete date horizon")
        if len(self.daily_coverage) != self.meal_plan.horizon_days:
            raise ValueError("partial coverage evidence is required for every day")

        reduced = False
        for day, coverage in zip(self.meal_plan.days, self.daily_coverage):
            scale = float(coverage.portion_scale)
            if coverage.day_index != day.day_index:
                raise ValueError("partial coverage day indices must match the meal plan")
            if not 0 < scale <= 1:
                raise ValueError("partial daily portion scales must be within (0, 1]")
            reduced = reduced or scale < 1.0 - 1e-9
            if coverage.calories_ratio + 1e-9 < 0.60:
                raise ValueError("daily calorie coverage must be at least 60%")
            if coverage.protein_ratio + 1e-9 < 0.60:
                raise ValueError("daily protein coverage must be at least 60%")
            if tuple(meal.slot for meal in day.meals) != SLOT_ORDER:
                raise ValueError("a partial plan must retain all three daily meal slots")
            for meal in day.meals:
                if meal.household_member_count != self.household_member_count:
                    raise ValueError("every partial meal must retain the full household")
                if not math.isclose(meal.portion_scale, scale, abs_tol=1e-9):
                    raise ValueError("all meals in a day must use its recorded portion scale")
                expected = self.household_member_count * scale
                if not math.isclose(meal.full_serving_equivalent, expected, abs_tol=1e-9):
                    raise ValueError("partial meal serving equivalents must match its scale")
        if not reduced:
            raise ValueError("a partial candidate must reduce at least one day")

        payload_plan = getattr(self.candidate, "meal_plan", self.meal_plan)
        if payload_plan != self.meal_plan:
            raise ValueError("the partial candidate payload must contain the same meal plan")

    @property
    def daily_portion_scales(self) -> tuple[float, ...]:
        return tuple(float(day.portion_scale) for day in self.daily_coverage)

    @property
    def minimum_daily_calorie_coverage(self) -> float:
        return min(float(day.calories_ratio) for day in self.daily_coverage)

    @property
    def minimum_daily_protein_coverage(self) -> float:
        return min(float(day.protein_ratio) for day in self.daily_coverage)

    @property
    def portion_scale(self) -> float:
        """Backward-compatible view of the smallest daily portion scale."""

        return min(self.daily_portion_scales)

    @property
    def full_serving_equivalent(self) -> float:
        """Backward-compatible minimum full-serving equivalent per meal."""

        return self.household_member_count * self.portion_scale

    @property
    def next_increment_additional_cents(self) -> int | None:
        """Extra cents required by the cheapest remaining one-percent step."""

        if self.next_increment_total_cents is None:
            return None
        return self.next_increment_total_cents - self.estimated_total_cents


PlanningOutcome: TypeAlias = (
    StandardPlanReady
    | BudgetChoiceRequired
    | NoPlanFoundWithinSearchLimits
    | NoFeasiblePlanProven
    | DataUnavailable
    | PartialFoodCoverageCandidate
)


def is_saveable_outcome(outcome: PlanningOutcome, *, partial_confirmed: bool = False) -> bool:
    """Only standard plans and explicitly confirmed partial candidates save."""

    return isinstance(outcome, StandardPlanReady) or (
        partial_confirmed and isinstance(outcome, PartialFoodCoverageCandidate)
    )
