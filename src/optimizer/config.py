"""Optimizer configuration.

These weights form an MVP scoring model — a practical planning heuristic,
NOT an official nutrition scoring system. They are tuned so the acceptance
cases (e.g. a $50/week family basket) produce balanced, realistic results,
and every one of them is open to debate.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OptimizerConfig:
    # Adequacy weights per nutrient ratio (capped at 100% of target).
    weight_calories: float = 2.0
    weight_protein: float = 1.5
    weight_other: float = 1.0

    # Penalty: nutrients still below the deep-deficiency floor (50% of target).
    penalty_missing: float = 25.0
    missing_floor: float = 0.5

    # Penalty per food group short of the coverage target (5 of 6 groups).
    penalty_groups: float = 12.0
    group_target: int = 5

    # Penalty per distinct food short of the variety target.
    penalty_variety: float = 4.0
    family_min_distinct: int = 7  # households of 2+
    single_min_distinct: int = 5

    # Penalty for any single food above the dominance share of calories/cost.
    penalty_dominance: float = 40.0
    dominance_share: float = 0.35

    # Penalty for cost-weighted low-confidence prices.
    penalty_low_confidence: float = 8.0

    # Penalty for buying calories well past the target (waste guard).
    penalty_overshoot: float = 15.0
    overshoot_ratio: float = 1.15

    # Local search bound (sweeps are deterministic; no time-based cutoffs).
    max_sweeps: int = 3

    def min_distinct(self, total_members: int) -> int:
        return self.family_min_distinct if total_members >= 2 else self.single_min_distinct
