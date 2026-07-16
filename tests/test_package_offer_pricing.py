"""Package-specific offer optimization and deferred fallback contracts."""

from __future__ import annotations

from dataclasses import replace

from models import (
    BudgetStatus,
    Location,
    Nutrients,
    OptimizationResult,
    PackageOffer,
    PriceQuote,
    PriceSource,
)
from services.basket_builder import (
    minimum_cost_package_combination,
    offers_for_food,
)
from services.price_engine import PriceEngine
from services.price_providers import PriceProvider, SeedProvider
from services.price_providers.base import ProviderResult


def _offer(food, package_index: int, cents: int, offer_id: str) -> PackageOffer:
    package = food.package_options[package_index]
    return PackageOffer.for_catalog_package(
        food,
        package,
        price_cents=cents,
        source=PriceSource.KROGER_REAL_PRICE,
        store="Test store",
        matched_product_name=f"Test {package.label}",
        confidence=1.0,
        is_estimate=False,
        last_updated="2026-01-01T00:00:00",
        match_reason="test",
        offer_id=offer_id,
    )


def test_minimum_cost_combination_can_mix_packages(foods_by_id):
    eggs = foods_by_id["eggs_large"]  # 600 g dozen; 300 g half-dozen
    dozen = _offer(eggs, 0, 250, "dozen")
    half = _offer(eggs, 1, 140, "half")

    lines = minimum_cost_package_combination(eggs, 750, (dozen, half))

    assert {(line.offer_id, line.count) for line in lines} == {
        ("dozen", 1),
        ("half", 1),
    }
    assert sum(line.total_cost_cents for line in lines) == 390
    assert sum(line.grams for line in lines) >= 750


def test_ties_choose_less_waste_then_fewer_packages(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    dozen = _offer(eggs, 0, 200, "dozen")
    half = _offer(eggs, 1, 100, "half")

    # Both one dozen and two half-dozens cost $2 and cover 600 g; one package wins.
    lines = minimum_cost_package_combination(eggs, 500, (half, dozen))
    assert [(line.offer_id, line.count) for line in lines] == [("dozen", 1)]

    # At equal cost, a 600 g package beats a more wasteful 1,200 g offer.
    jumbo = PackageOffer(
        offer_id="jumbo",
        package_id="jumbo-package",
        food_id=eggs.id,
        package_label="2 dozen",
        package_grams=1200,
        price_cents=200,
        source=PriceSource.KROGER_REAL_PRICE,
        store="Test store",
        matched_product_name="Jumbo eggs",
        confidence=1.0,
        is_estimate=False,
        last_updated="2026-01-01T00:00:00",
        match_reason="test",
    )
    lines = minimum_cost_package_combination(eggs, 500, (jumbo, dozen))
    assert [(line.offer_id, line.count) for line in lines] == [("dozen", 1)]


def test_stable_offer_order_and_exact_triple_deduplication(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    first = _offer(eggs, 0, 200, "a-offer")
    second = _offer(eggs, 0, 200, "b-offer")

    normalized = offers_for_food(eggs, (second, first, first))
    assert [offer.offer_id for offer in normalized] == ["a-offer", "b-offer"]
    lines = minimum_cost_package_combination(eggs, 500, normalized)
    assert [(line.offer_id, line.count) for line in lines] == [("a-offer", 1)]


def test_source_mix_uses_native_offer_provenance(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    line = minimum_cost_package_combination(
        eggs, 500, (_offer(eggs, 0, 200, "native-offer"),)
    )[0]
    # A quote is only the legacy/presentation adapter.  Even if reconstructed
    # with different provenance, the package offer remains authoritative.
    line = replace(
        line,
        quote=replace(line.quote, source=PriceSource.BLS_REGIONAL_AVERAGE),
    )
    result = OptimizationResult(
        items=(line,),
        total_cost=2.0,
        budget=3.0,
        score=0.0,
        nutrient_totals=Nutrients(),
        gaps=(),
        group_coverage={},
        groups_covered=0,
        distinct_foods=1,
        budget_status=BudgetStatus.WITHIN,
        nutrition_feasible=True,
        relaxed_constraints=(),
        dominance_flags=(),
    )
    assert result.source_mix == {PriceSource.KROGER_REAL_PRICE: 1}


def test_offer_rejects_zero_price(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    try:
        _offer(eggs, 0, 0, "free")
    except ValueError as exc:
        assert "positive" in str(exc)
    else:  # pragma: no cover - documents the data-quality invariant
        raise AssertionError("zero-price package offer was accepted")


class _QuoteProvider(PriceProvider):
    def __init__(self, source: PriceSource, quote: PriceQuote):
        self.source = source
        self.name = source.value
        self.quote = quote
        self.calls = 0

    async def get_quote(self, food, location):
        self.calls += 1
        return ProviderResult(quote=self.quote)


def _quote(food, source: PriceSource, store: str) -> PriceQuote:
    package = food.package_options[0]
    return PriceQuote(
        food_name=food.name,
        matched_product_name=f"{store} {food.name}",
        price=4.00,
        unit=package.label,
        unit_price=4.00,
        normalized_unit_price=4.00 / (package.grams / 100.0),
        raw_unit=package.label,
        normalized_unit="100g",
        store=store,
        source=source,
        confidence=0.9,
        is_estimate=source is PriceSource.BLS_REGIONAL_AVERAGE,
        last_updated="2026-01-01T00:00:00",
        match_reason="test",
    )


async def test_seed_fallback_is_deferred_and_diagnostic(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    engine = PriceEngine([SeedProvider()])
    location = Location(city="LA", zip_code="90001")

    market = await engine.get_package_offers(eggs, location)
    assert market.offers == ()
    assert not market.diagnostics.local_fallback_used

    fallback = await engine.get_package_offers(
        eggs, location, allow_local_fallback=True
    )
    assert len(fallback.offers) == len(eggs.package_options)
    assert fallback.diagnostics.local_fallback_used
    assert fallback.diagnostics.fallback_food_ids == (eggs.id,)


async def test_seed_never_overwrites_live_or_bls_offer(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    location = Location(city="LA", zip_code="90001")
    live = _QuoteProvider(
        PriceSource.KROGER_REAL_PRICE,
        _quote(eggs, PriceSource.KROGER_REAL_PRICE, "Live store"),
    )
    engine = PriceEngine([live, SeedProvider()])
    lookup = await engine.get_package_offers(
        eggs, location, allow_local_fallback=True
    )
    assert {offer.source for offer in lookup.offers} == {
        PriceSource.KROGER_REAL_PRICE
    }
    assert not lookup.diagnostics.local_fallback_used

    bls = _QuoteProvider(
        PriceSource.BLS_REGIONAL_AVERAGE,
        _quote(eggs, PriceSource.BLS_REGIONAL_AVERAGE, "BLS"),
    )
    engine = PriceEngine([bls, SeedProvider()])
    lookup = await engine.get_package_offers(
        eggs, location, allow_local_fallback=True
    )
    assert {offer.source for offer in lookup.offers} == {
        PriceSource.BLS_REGIONAL_AVERAGE
    }
    assert len(lookup.offers) == len(eggs.package_options)
    assert {offer.package_id for offer in lookup.offers} == {
        package.package_id for package in eggs.package_options
    }
    assert not lookup.diagnostics.local_fallback_used
