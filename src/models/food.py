"""Food domain models: preparation states, food groups, nutrients, packages."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import ClassVar


class PrepState(str, Enum):
    """Preparation state of a seed food as purchased.

    Nutrient values are always stored on the same basis as the purchased
    quantity (e.g. dry rice nutrients per 100 g of dry rice).
    """

    RAW = "raw"
    COOKED = "cooked"
    CANNED = "canned"
    PREPARED = "prepared"


class FoodGroup(str, Enum):
    """The six weekly food groups RightMeal plans around."""

    GRAINS_STARCHY = "grains_starchy"
    PROTEIN = "protein"
    VEGETABLES = "vegetables"
    FRUITS = "fruits"
    DAIRY_FORTIFIED_ALT = "dairy_fortified_alt"
    HEALTHY_FATS = "healthy_fats"


FOOD_GROUP_LABELS: dict[FoodGroup, str] = {
    FoodGroup.GRAINS_STARCHY: "Grains & starchy foods",
    FoodGroup.PROTEIN: "Protein foods",
    FoodGroup.VEGETABLES: "Vegetables",
    FoodGroup.FRUITS: "Fruits",
    FoodGroup.DAIRY_FORTIFIED_ALT: "Dairy or fortified alternatives",
    FoodGroup.HEALTHY_FATS: "Healthy fats",
}


@dataclass(frozen=True)
class Nutrients:
    """Amounts of the 12 tracked nutrients, per 100 g of edible food."""

    calories_kcal: float = 0.0
    protein_g: float = 0.0
    fiber_g: float = 0.0
    calcium_mg: float = 0.0
    iron_mg: float = 0.0
    potassium_mg: float = 0.0
    vitamin_a_mcg_rae: float = 0.0
    vitamin_c_mg: float = 0.0
    vitamin_d_mcg: float = 0.0
    folate_mcg_dfe: float = 0.0
    magnesium_mg: float = 0.0
    zinc_mg: float = 0.0

    NAMES: ClassVar[tuple[str, ...]] = (
        "calories_kcal",
        "protein_g",
        "fiber_g",
        "calcium_mg",
        "iron_mg",
        "potassium_mg",
        "vitamin_a_mcg_rae",
        "vitamin_c_mg",
        "vitamin_d_mcg",
        "folate_mcg_dfe",
        "magnesium_mg",
        "zinc_mg",
    )

    NUTRIENT_LABELS: ClassVar[dict[str, str]] = {
        "calories_kcal": "Calories",
        "protein_g": "Protein",
        "fiber_g": "Fiber",
        "calcium_mg": "Calcium",
        "iron_mg": "Iron",
        "potassium_mg": "Potassium",
        "vitamin_a_mcg_rae": "Vitamin A",
        "vitamin_c_mg": "Vitamin C",
        "vitamin_d_mcg": "Vitamin D",
        "folate_mcg_dfe": "Folate",
        "magnesium_mg": "Magnesium",
        "zinc_mg": "Zinc",
    }

    def get(self, name: str) -> float:
        return getattr(self, name)

    def scaled(self, factor: float) -> "Nutrients":
        return Nutrients(**{n: getattr(self, n) * factor for n in self.NAMES})

    def plus(self, other: "Nutrients") -> "Nutrients":
        return Nutrients(**{n: getattr(self, n) + getattr(other, n) for n in self.NAMES})

    def as_dict(self) -> dict[str, float]:
        return {n: getattr(self, n) for n in self.NAMES}

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> "Nutrients":
        unknown = set(data) - set(cls.NAMES)
        if unknown:
            raise ValueError(f"Unknown nutrient fields: {sorted(unknown)}")
        return cls(**{n: float(data.get(n, 0.0)) for n in cls.NAMES})


# Keep NAMES in sync with the dataclass fields.
assert Nutrients.NAMES == tuple(f.name for f in fields(Nutrients)), (
    "Nutrients.NAMES out of sync with dataclass fields"
)


@dataclass(frozen=True)
class PackageOption:
    """A purchasable package size of a food (e.g. '1 gallon', 'half dozen')."""

    label: str
    grams: float
    seed_price: float
    ml: float | None = None  # set for liquids; price normalization uses per-100ml

    def __post_init__(self) -> None:
        if self.grams <= 0:
            raise ValueError(f"Package '{self.label}' must have positive grams")
        if self.seed_price < 0:
            raise ValueError(f"Package '{self.label}' must have a non-negative price")
        if self.ml is not None and self.ml <= 0:
            raise ValueError(f"Package '{self.label}' ml must be positive when set")


@dataclass(frozen=True)
class Food:
    """A curated seed food with nutrition, packaging, and dietary metadata."""

    id: str
    name: str
    food_group: FoodGroup
    prep_state: PrepState
    form: str  # free text: "fresh", "dry", "frozen", "canned", ...
    fdc_id: int | None
    is_liquid: bool
    density_g_per_ml: float | None
    package_options: tuple[PackageOption, ...]
    max_weekly_grams: float  # per household member per 7 days
    allergen_tags: frozenset[str]
    lactose: bool
    vegetarian: bool
    vegan: bool
    contains_pork: bool
    is_meat_or_fish: bool
    search_terms: tuple[str, ...]
    nutrients_per_100g: Nutrients
    edible_fraction: float = 1.0  # edible share of purchased weight (peels, cores)
    image_url: str | None = None  # small ingredient photo (web; cached locally)
    # Cooked weight / dry (or raw) weight, for display of plate weights of dry
    # foods (rice, pasta, lentils, ...). None when not applicable.
    cooked_yield_factor: float | None = None
    # Daily grams one member can realistically eat/drink of this food; caps
    # optimizer purchases so it can't buy more than the meal plan can plate.
    # Set only for foods with a limited plate role (beverages today).
    # Leave None for staples.
    max_plated_grams_per_member_day: float | None = None

    def __post_init__(self) -> None:
        if not self.package_options:
            raise ValueError(f"Food '{self.id}' must have at least one package option")
        if self.is_liquid:
            if self.density_g_per_ml is None or self.density_g_per_ml <= 0:
                raise ValueError(f"Liquid food '{self.id}' must have a positive density")
            for pkg in self.package_options:
                if pkg.ml is None:
                    raise ValueError(
                        f"Liquid food '{self.id}' package '{pkg.label}' must specify ml"
                    )
        if not 0 < self.edible_fraction <= 1:
            raise ValueError(f"Food '{self.id}' edible_fraction must be in (0, 1]")
        if self.max_weekly_grams <= 0:
            raise ValueError(f"Food '{self.id}' max_weekly_grams must be positive")
        if self.max_plated_grams_per_member_day is not None and self.max_plated_grams_per_member_day <= 0:
            raise ValueError(
                f"Food '{self.id}' max_plated_grams_per_member_day must be positive when set"
            )

    def seed_cost_per_100(self, pkg: PackageOption) -> float:
        """Seed cost per 100 g (solids) or per 100 ml (liquids) for a package."""
        if self.is_liquid:
            assert pkg.ml is not None
            return pkg.seed_price / (pkg.ml / 100.0)
        return pkg.seed_price / (pkg.grams / 100.0)

    @property
    def smallest_package(self) -> PackageOption:
        return min(self.package_options, key=lambda p: p.grams)

    def nutrients_per_purchased_100g(self) -> Nutrients:
        """Nutrients per 100 g as purchased, accounting for inedible share."""
        return self.nutrients_per_100g.scaled(self.edible_fraction)
