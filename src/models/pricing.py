"""Pricing models: sources, package offers, quotes, and locations.

``PriceQuote`` is the legacy, normalized per-food view used by the current UI.
New planning code should use :class:`PackageOffer`: an offer is bound to one
stable package identity and stores money as integer cents.  Keeping the legacy
model here makes the pricing migration additive instead of forcing UI and saved
plan migrations to land in the same change.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import TYPE_CHECKING
import uuid

if TYPE_CHECKING:
    from models.food import Food, PackageOption


class PriceSource(str, Enum):
    """Where a price quote came from, in fallback priority order."""

    KROGER_REAL_PRICE = "kroger_real_price"
    INSTACART_NUMERIC_PRICE = "instacart_numeric_price"
    BLS_REGIONAL_AVERAGE = "bls_regional_average"
    SEED_ESTIMATE = "seed_estimate"


PRICE_SOURCE_LABELS: dict[PriceSource, str] = {
    PriceSource.KROGER_REAL_PRICE: "Kroger/Ralphs real price",
    PriceSource.INSTACART_NUMERIC_PRICE: "Instacart numeric product price",
    PriceSource.BLS_REGIONAL_AVERAGE: "BLS regional average estimate",
    PriceSource.SEED_ESTIMATE: "Seed estimate",
}


@dataclass(frozen=True)
class Location:
    city: str
    zip_code: str


@dataclass(frozen=True)
class PriceQuote:
    """A normalized price for one food from one provider.

    ``normalized_unit_price`` is cost per 100 g for solid foods and cost per
    100 ml for liquids; ``normalized_unit`` says which basis applies.
    """

    food_name: str
    matched_product_name: str
    price: float
    unit: str
    unit_price: float
    normalized_unit_price: float
    raw_unit: str
    normalized_unit: str  # "100g" | "100ml"
    store: str
    source: PriceSource
    confidence: float
    is_estimate: bool
    last_updated: str  # ISO 8601
    match_reason: str
    provider_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, PriceSource):
            raise ValueError(f"source must be a PriceSource, got {self.source!r}")
        if self.normalized_unit not in ("100g", "100ml"):
            raise ValueError(f"normalized_unit must be '100g' or '100ml', got {self.normalized_unit!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0, 1], got {self.confidence}")
        if self.price < 0 or self.unit_price < 0 or self.normalized_unit_price < 0:
            raise ValueError("prices must be non-negative")


_PRICING_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "rightmeal.local/pricing")


def dollars_to_cents(value: object) -> int:
    """Convert a dollar value to cents without binary-float rounding drift."""

    try:
        amount = Decimal(str(value))
    except Exception as exc:  # Decimal raises several input-specific errors
        raise ValueError(f"invalid dollar amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError("dollar amount must be finite")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def package_id_for(food_id: str, package: object) -> str:
    """Return a package's persisted id, with a deterministic legacy fallback.

    ``PackageOption.package_id`` is being introduced independently.  This
    helper deliberately accepts the old shape so package-offer work remains
    compatible while that schema change is in flight.
    """

    explicit = str(getattr(package, "package_id", "") or "").strip()
    if explicit:
        return explicit
    try:
        from models.food import PackageOption, deterministic_package_id

        if isinstance(package, PackageOption):
            return deterministic_package_id(food_id, package)
    except ImportError:  # defensive during an import cycle in tooling
        pass
    label = str(getattr(package, "label", "")).strip().casefold()
    grams = Decimal(str(getattr(package, "grams", 0))).normalize()
    ml_value = getattr(package, "ml", None)
    ml = "" if ml_value is None else str(Decimal(str(ml_value)).normalize())
    identity = f"package|{food_id}|{label}|{grams}|{ml}"
    return str(uuid.uuid5(_PRICING_NAMESPACE, identity))


def _stable_offer_id(
    *,
    food_id: str,
    package_id: str,
    source: PriceSource,
    store: str,
    matched_product_name: str,
    raw_unit: str,
) -> str:
    # Price and timestamp are observations, not identity: a retailer offer keeps
    # the same id when its price changes.
    identity = "|".join(
        (
            "offer",
            food_id,
            package_id,
            source.value,
            store.strip().casefold(),
            matched_product_name.strip().casefold(),
            raw_unit.strip().casefold(),
        )
    )
    return str(uuid.uuid5(_PRICING_NAMESPACE, identity))


@dataclass(frozen=True)
class PackageOffer:
    """One concrete whole-package price offer.

    Offer and package identity are stable; ``price_cents`` is the only money
    representation used by the package optimizer.  Snapshot fields make an
    offer self-contained even if a retailer result is no longer available.
    """

    offer_id: str
    package_id: str
    food_id: str
    package_label: str
    package_grams: float
    price_cents: int
    source: PriceSource
    store: str
    matched_product_name: str
    confidence: float
    is_estimate: bool
    last_updated: str
    match_reason: str
    package_ml: float | None = None
    raw_unit: str = ""
    provider_error: str | None = None

    def __post_init__(self) -> None:
        if not self.offer_id or not self.package_id or not self.food_id:
            raise ValueError("offer_id, package_id, and food_id are required")
        if not self.package_label.strip():
            raise ValueError("package_label is required")
        if self.package_grams <= 0:
            raise ValueError("package_grams must be positive")
        if isinstance(self.price_cents, bool) or not isinstance(self.price_cents, int):
            raise ValueError("price_cents must be an integer")
        # Unknown and zero prices are data-quality failures, never free offers.
        if self.price_cents <= 0:
            raise ValueError("price_cents must be positive")
        if not isinstance(self.source, PriceSource):
            raise ValueError(f"source must be a PriceSource, got {self.source!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        if self.package_ml is not None and self.package_ml <= 0:
            raise ValueError("package_ml must be positive when set")

    @property
    def price(self) -> float:
        """Legacy-friendly dollar view; optimizer comparisons stay in cents."""

        return self.price_cents / 100.0

    @classmethod
    def for_catalog_package(
        cls,
        food: "Food",
        package: "PackageOption",
        *,
        price_cents: int,
        source: PriceSource,
        store: str,
        matched_product_name: str | None = None,
        confidence: float = 1.0,
        is_estimate: bool = True,
        last_updated: str = "",
        match_reason: str = "",
        raw_unit: str | None = None,
        provider_error: str | None = None,
        offer_id: str | None = None,
    ) -> "PackageOffer":
        package_id = package_id_for(food.id, package)
        product = matched_product_name or food.name
        raw = raw_unit or package.label
        stable_id = offer_id or _stable_offer_id(
            food_id=food.id,
            package_id=package_id,
            source=source,
            store=store,
            matched_product_name=product,
            raw_unit=raw,
        )
        return cls(
            offer_id=stable_id,
            package_id=package_id,
            food_id=food.id,
            package_label=package.label,
            package_grams=float(package.grams),
            package_ml=(float(package.ml) if package.ml is not None else None),
            price_cents=price_cents,
            source=source,
            store=store,
            matched_product_name=product,
            confidence=confidence,
            is_estimate=is_estimate,
            last_updated=last_updated,
            match_reason=match_reason,
            raw_unit=raw,
            provider_error=provider_error,
        )

    @classmethod
    def from_quote(cls, food: "Food", quote: PriceQuote) -> "PackageOffer":
        """Bind a legacy quote to its best matching catalog package.

        Providers currently expose one normalized quote.  Exact label matches
        win; otherwise the normalized price implies a package amount and the
        closest catalog package supplies the stable package identity.  Native
        package-offer providers can bypass this adapter entirely.
        """

        if quote.price <= 0 or quote.normalized_unit_price <= 0:
            raise ValueError("a quote needs a positive package and normalized price")
        packages = tuple(food.package_options)
        if not packages:
            raise ValueError(f"food {food.id!r} has no package data")
        labels = {quote.unit.strip().casefold(), quote.raw_unit.strip().casefold()}
        exact = [p for p in packages if p.label.strip().casefold() in labels]
        if exact:
            package = min(exact, key=lambda p: package_id_for(food.id, p))
        else:
            if quote.normalized_unit == "100ml":
                implied_ml = quote.price / quote.normalized_unit_price * 100.0
                implied_grams = implied_ml * float(food.density_g_per_ml or 1.0)
            else:
                implied_grams = quote.price / quote.normalized_unit_price * 100.0
            package = min(
                packages,
                key=lambda p: (
                    abs(float(p.grams) - implied_grams),
                    package_id_for(food.id, p),
                ),
            )
        return cls.for_catalog_package(
            food,
            package,
            price_cents=dollars_to_cents(quote.price),
            source=quote.source,
            store=quote.store,
            matched_product_name=quote.matched_product_name,
            confidence=quote.confidence,
            is_estimate=quote.is_estimate,
            last_updated=quote.last_updated,
            match_reason=quote.match_reason,
            raw_unit=quote.raw_unit,
            provider_error=quote.provider_error,
        )

    def to_quote(self, food: "Food") -> PriceQuote:
        """Compatibility projection for UI/explanation code not yet migrated."""

        if food.is_liquid and self.package_ml is not None:
            normalized_unit = "100ml"
            per_100 = self.price / (self.package_ml / 100.0)
        else:
            normalized_unit = "100g"
            per_100 = self.price / (self.package_grams / 100.0)
        return PriceQuote(
            food_name=food.name,
            matched_product_name=self.matched_product_name,
            price=self.price,
            unit=self.package_label,
            unit_price=self.price,
            normalized_unit_price=per_100,
            raw_unit=self.raw_unit or self.package_label,
            normalized_unit=normalized_unit,
            store=self.store,
            source=self.source,
            confidence=self.confidence,
            is_estimate=self.is_estimate,
            last_updated=self.last_updated,
            match_reason=self.match_reason,
            provider_error=self.provider_error,
        )


def seed_package_offers(food: "Food", *, last_updated: str = "") -> tuple[PackageOffer, ...]:
    """Every positive-price catalog package as a separate seed offer."""

    offers: list[PackageOffer] = []
    for package in food.package_options:
        cents = dollars_to_cents(package.seed_price)
        if cents <= 0:
            continue
        offers.append(
            PackageOffer.for_catalog_package(
                food,
                package,
                price_cents=cents,
                source=PriceSource.SEED_ESTIMATE,
                store="Seed data",
                matched_product_name=food.name,
                confidence=1.0,
                is_estimate=True,
                last_updated=last_updated,
                match_reason="curated seed estimate",
            )
        )
    return tuple(sorted(offers, key=lambda offer: offer.offer_id))


@dataclass(frozen=True)
class PriceLookupDiagnostics:
    """Non-sensitive provenance for deferred local fallback."""

    provider_failures: tuple[str, ...] = ()
    local_fallback_used: bool = False
    fallback_food_ids: tuple[str, ...] = ()
    fallback_sources: tuple[PriceSource, ...] = ()

    @property
    def local_fallback_food_count(self) -> int:
        return len(self.fallback_food_ids)


@dataclass(frozen=True)
class OfferLookup:
    offers: tuple[PackageOffer, ...]
    diagnostics: PriceLookupDiagnostics = PriceLookupDiagnostics()


@dataclass(frozen=True)
class OfferBook:
    offers_by_food: dict[str, tuple[PackageOffer, ...]]
    missing_food_ids: tuple[str, ...]
    diagnostics: PriceLookupDiagnostics

    def offers_for(self, food_id: str) -> tuple[PackageOffer, ...]:
        return self.offers_by_food.get(food_id, ())
