"""Structured explanation of an optimization result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Explanation:
    """Human-readable explanation of a verified basket.

    Explanations describe the basket; they never choose foods, make medical
    claims, or promise health outcomes.
    """

    summary: str
    item_reasons: dict[str, str] = field(default_factory=dict)  # food name -> reason
    nutrition_gaps: list[str] = field(default_factory=list)
    budget_tradeoffs: str = ""
    food_group_coverage: str = ""
    life_impact: str = ""
    generated_by: Literal["openai", "local"] = "local"
