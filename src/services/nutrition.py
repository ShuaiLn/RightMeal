"""Household nutrition targets, basket totals, gaps, and group coverage."""

from __future__ import annotations

from typing import Iterable, Sequence

from models.basket import BasketItem, NutrientGap
from models.food import FoodGroup, Nutrients
from models.profile import HouseholdProfile


class NutritionService:
    def __init__(self, targets_data: dict):
        self._person_types = {
            name: Nutrients.from_dict(values)
            for name, values in targets_data["person_types"].items()
        }
        self._group_caps = {
            FoodGroup(group): float(grams)
            for group, grams in targets_data["group_weekly_caps_g_per_person"].items()
        }

    def household_daily_targets(self, profile: HouseholdProfile) -> Nutrients:
        total = Nutrients()
        for person_type, count in (
            ("adult", profile.adults),
            ("child", profile.children),
            ("senior", profile.seniors),
        ):
            if count > 0:
                total = total.plus(self._person_types[person_type].scaled(count))
        return total

    def household_targets(self, profile: HouseholdProfile, horizon_days: int = 7) -> Nutrients:
        """Household nutrition targets for the planning horizon."""
        return self.household_daily_targets(profile).scaled(horizon_days)

    @staticmethod
    def basket_totals(items: Iterable[BasketItem]) -> Nutrients:
        total = Nutrients()
        for item in items:
            total = total.plus(item.nutrients)
        return total

    @staticmethod
    def coverage(totals: Nutrients, targets: Nutrients) -> list[NutrientGap]:
        """Achieved vs. target for every tracked nutrient."""
        return [
            NutrientGap(nutrient=name, achieved=totals.get(name), target=targets.get(name))
            for name in Nutrients.NAMES
        ]

    @staticmethod
    def gaps(totals: Nutrients, targets: Nutrients) -> list[NutrientGap]:
        """Nutrients below 100% of target."""
        return [g for g in NutritionService.coverage(totals, targets) if g.pct < 100.0]

    @staticmethod
    def group_coverage(items: Sequence[BasketItem]) -> dict[FoodGroup, float]:
        grams: dict[FoodGroup, float] = {}
        for item in items:
            group = item.food.food_group
            grams[group] = grams.get(group, 0.0) + item.grams
        return grams

    def group_caps_g(self, profile: HouseholdProfile, horizon_days: int = 7) -> dict[FoodGroup, float]:
        factor = profile.total_members * horizon_days / 7.0
        return {group: cap * factor for group, cap in self._group_caps.items()}
