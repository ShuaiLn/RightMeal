"""Household nutrition targets, basket totals, gaps, and group coverage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from models.basket import BasketItem, NutrientGap
from models.food import Food, FoodGroup, Nutrients
from models.profile import HouseholdProfile
from services.dietary import exclusion_reason


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


@dataclass(frozen=True)
class NutrientStatus:
    """One nutrient's coverage for a day's actually-eaten meals vs. target."""

    nutrient: str
    pct: float | None  # None when target is 0 (no divide-by-zero, no fake 0%)
    level: str  # "sufficient" | "borderline" | "lacking"


def eaten_day_status(eaten: Nutrients, targets: Nutrients) -> list[NutrientStatus]:
    """Coverage of what was actually eaten in a day against the household's
    daily targets. All 12 tracked nutrients are "more is better, up to
    target" — this app tracks no upper-bound nutrient (e.g. sodium) — so a
    single >=100% / >=70% threshold direction is correct for every nutrient.
    """
    statuses: list[NutrientStatus] = []
    for name in Nutrients.NAMES:
        target = targets.get(name)
        if target <= 0:
            statuses.append(NutrientStatus(name, None, "sufficient"))
            continue
        pct = 100.0 * eaten.get(name) / target
        level = "sufficient" if pct >= 100 else "borderline" if pct >= 70 else "lacking"
        statuses.append(NutrientStatus(name, pct, level))
    return statuses


def suggest_foods_for(
    nutrient: str, foods: Sequence[Food], profile: HouseholdProfile, limit: int = 3
) -> list[Food]:
    """The top foods for one nutrient that fit the household's dietary
    restrictions — filtered through the same exclusion rules the basket
    builder and recipe validator use, so a suggestion never crosses an
    allergy or diet restriction."""
    allowed = [food for food in foods if exclusion_reason(food, profile) is None]
    allowed.sort(key=lambda food: food.nutrients_per_100g.get(nutrient), reverse=True)
    return allowed[:limit]
