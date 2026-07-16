"""Food domain models: preparation states, food groups, nutrients, packages."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from models.quantities import grams_decimal, quantity_decimal


PACKAGE_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "rightmeal.local/package")


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
    # Catalog package identity.  Existing curated data omits this field; Food
    # backfills it deterministically after the owning food id is known.
    package_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "package_id", str(self.package_id).strip())
        if self.grams <= 0:
            raise ValueError(f"Package '{self.label}' must have positive grams")
        if self.seed_price < 0:
            raise ValueError(f"Package '{self.label}' must have a non-negative price")
        if self.ml is not None and self.ml <= 0:
            raise ValueError(f"Package '{self.label}' ml must be positive when set")


def deterministic_package_id(food_id: str, package: PackageOption) -> str:
    """Stable id for catalog packages that predate explicit ids.

    Labels remain display text.  Weight/volume are included so a later catalog
    package that happens to reuse a label cannot silently take over the old
    package's identity.
    """

    grams = format(grams_decimal(package.grams).normalize(), "f")
    ml = "" if package.ml is None else format(grams_decimal(package.ml).normalize(), "f")
    fingerprint = f"{food_id}\x1f{package.label}\x1f{grams}\x1f{ml}"
    return str(uuid.uuid5(PACKAGE_ID_NAMESPACE, fingerprint))


@dataclass(frozen=True)
class PackageUnit:
    """A package conversion bound to one exact catalog food and package.

    The binding is deliberately revalidated against the current ``Food`` on
    every conversion.  A unit retained by a UI after the selected food changes
    therefore becomes invalid immediately instead of silently reusing a weight
    from the previous food.
    """

    food_id: str
    package_label: str
    grams: Decimal
    package_id: str = ""

    def __post_init__(self) -> None:
        if not self.food_id or not self.package_label:
            raise ValueError("a package unit needs a food and package label")
        object.__setattr__(self, "grams", grams_decimal(self.grams, positive=True))
        object.__setattr__(self, "package_id", str(self.package_id).strip())

    @classmethod
    def from_option(cls, food: "Food", package: PackageOption) -> "PackageUnit":
        if not any(
            candidate.package_id == package.package_id
            and candidate.label == package.label
            and grams_decimal(candidate.grams) == grams_decimal(package.grams)
            for candidate in food.package_options
        ):
            raise ValueError("the package does not belong to the selected food")
        return cls(
            food.id,
            package.label,
            grams_decimal(package.grams, positive=True),
            package.package_id,
        )

    def option_for(self, food: "Food") -> PackageOption:
        if food.id != self.food_id:
            raise ValueError("the package unit belongs to a different food")
        for package in food.package_options:
            if (
                (not self.package_id or package.package_id == self.package_id)
                and package.label == self.package_label
                and grams_decimal(package.grams) == self.grams
            ):
                return package
        raise ValueError("the package is no longer valid for the selected food")

    def to_grams(self, quantity: object, food: "Food") -> float:
        self.option_for(food)
        normalized_quantity = quantity_decimal(quantity, positive=True)
        return float(grams_decimal(normalized_quantity * self.grams, positive=True))


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
        packages = tuple(
            package
            if package.package_id
            else replace(package, package_id=deterministic_package_id(self.id, package))
            for package in self.package_options
        )
        package_ids = [package.package_id for package in packages]
        if len(package_ids) != len(set(package_ids)):
            raise ValueError(f"Food '{self.id}' has duplicate package ids")
        object.__setattr__(self, "package_options", packages)
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
