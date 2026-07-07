"""Explanation service interface.

Explanation services only describe a verified optimizer result. They never
choose foods, override budget/nutrition validation, make medical claims, or
produce purchase links. The optimizer output must stay understandable without
any AI involved.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.basket import OptimizationResult
from models.explanation import Explanation
from models.profile import HouseholdProfile


class ExplanationService(ABC):
    @abstractmethod
    async def explain(self, result: OptimizationResult, profile: HouseholdProfile) -> Explanation:
        """Produce a structured, human-readable explanation of the result."""
