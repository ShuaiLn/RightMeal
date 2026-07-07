"""Deterministic template-based explanations — the always-available fallback.

Uses coverage language ("covers 74% of the target"), never medical claims,
diagnosis, treatment language, or promises of health outcomes.
"""

from __future__ import annotations

from models.basket import BasketItem, OptimizationResult
from models.explanation import Explanation
from models.food import FOOD_GROUP_LABELS, FoodGroup, Nutrients
from models.profile import HouseholdProfile
from services.explanation.base import ExplanationService

APPROX_KCAL_PER_MEAL = 600


class LocalExplanationService(ExplanationService):
    async def explain(self, result: OptimizationResult, profile: HouseholdProfile) -> Explanation:
        return Explanation(
            summary=self._summary(result, profile),
            item_reasons=self._item_reasons(result),
            nutrition_gaps=self._nutrition_gaps(result),
            budget_tradeoffs=self._budget_tradeoffs(result),
            food_group_coverage=self._food_group_coverage(result),
            life_impact=self._life_impact(result, profile),
            generated_by="local",
        )

    @staticmethod
    def _summary(result: OptimizationResult, profile: HouseholdProfile) -> str:
        if not result.budget_feasible:
            return (
                f"No food package fits the ${result.budget:.2f} budget for "
                f"{result.horizon_days} days. Try a higher budget or a shorter planning period."
            )
        base = (
            f"This basket has an estimated planning total of ${result.total_cost:.2f} "
            f"(budget ${result.budget:.2f}) and covers {result.groups_covered} of 6 food "
            f"groups with {result.distinct_foods} different foods for your household of "
            f"{profile.total_members} over {result.horizon_days} days."
        )
        if result.nutrition_feasible:
            return base + " All planning targets were met."
        return base + " Some nutrition targets could not be met within this budget — see the gaps below."

    @staticmethod
    def _item_reasons(result: OptimizationResult) -> dict[str, str]:
        totals = result.nutrient_totals
        reasons: dict[str, str] = {}
        for item in result.items:
            reasons[item.food.name] = LocalExplanationService._reason_for(item, totals)
        return reasons

    @staticmethod
    def _reason_for(item: BasketItem, totals: Nutrients) -> str:
        contributions: list[tuple[float, str]] = []
        item_nutrients = item.nutrients
        for name in Nutrients.NAMES:
            total = totals.get(name)
            if total > 0:
                share = item_nutrients.get(name) / total
                contributions.append((share, Nutrients.NUTRIENT_LABELS[name].lower()))
        contributions.sort(key=lambda pair: (-pair[0], pair[1]))
        top = [f"{share:.0%} of the basket's {label}" for share, label in contributions[:2] if share >= 0.005]
        role = " and ".join(top) if top else "variety across food groups"
        return f"{item.quantity_label} for ${item.cost:.2f} — provides {role}."

    @staticmethod
    def _nutrition_gaps(result: OptimizationResult) -> list[str]:
        return [
            f"Covers {gap.pct:.0f}% of the {result.horizon_days}-day "
            f"{Nutrients.NUTRIENT_LABELS[gap.nutrient]} target."
            for gap in result.gaps
        ]

    @staticmethod
    def _budget_tradeoffs(result: OptimizationResult) -> str:
        parts: list[str] = []
        if result.budget_feasible:
            parts.append(
                f"${result.total_cost:.2f} of the ${result.budget:.2f} budget is used "
                f"(prices are planning estimates from mixed sources, not one store's checkout total)."
            )
        parts.extend(result.relaxed_constraints)
        if result.dominance_flags:
            parts.append("Flagged items: " + "; ".join(result.dominance_flags) + ".")
        return " ".join(parts)

    @staticmethod
    def _food_group_coverage(result: OptimizationResult) -> str:
        covered = [FOOD_GROUP_LABELS[g] for g in FoodGroup if g in result.group_coverage]
        missing = [FOOD_GROUP_LABELS[g] for g in FoodGroup if g not in result.group_coverage]
        text = f"Covers {result.groups_covered} of 6 food groups"
        if covered:
            text += ": " + ", ".join(covered)
        text += "."
        if missing:
            text += " Not included: " + ", ".join(missing) + "."
        return text

    @staticmethod
    def _life_impact(result: OptimizationResult, profile: HouseholdProfile) -> str:
        if not result.items:
            return ""
        total_kcal = result.nutrient_totals.calories_kcal
        meals = max(1, round(total_kcal / APPROX_KCAL_PER_MEAL))
        per_meal = result.total_cost / meals
        return (
            f"Roughly {meals} home-prepared meal portions over {result.horizon_days} days "
            f"for your household of {profile.total_members} — about ${per_meal:.2f} per portion."
        )
