"""Price engine tests: fallback order, threshold, caching, source integrity."""

import httpx
import pytest

from data import load_bls_price_map
from models import Location, PriceQuote, PriceSource
from services.cache import SessionCache
from services.price_engine import MIN_CONFIDENCE, PriceEngine
from services.price_providers import (
    BlsProvider,
    InstacartProvider,
    KrogerProvider,
    PriceProvider,
    SeedProvider,
)
from services.price_providers.base import ProviderResult, now_iso

LA = Location(city="Los Angeles", zip_code="90001")


def make_quote(source: PriceSource, confidence: float = 0.9, price: float = 2.0) -> PriceQuote:
    return PriceQuote(
        food_name="Eggs, large",
        matched_product_name="Eggs",
        price=price,
        unit="1 dozen",
        unit_price=price,
        normalized_unit_price=price / 6.0,
        raw_unit="dozen",
        normalized_unit="100g",
        store="Test store",
        source=source,
        confidence=confidence,
        is_estimate=source is not PriceSource.KROGER_REAL_PRICE,
        last_updated=now_iso(),
        match_reason="test",
    )


class FakeProvider(PriceProvider):
    def __init__(self, name: str, source: PriceSource, result: ProviderResult, configured: bool = True):
        self.name = name
        self.source = source
        self._result = result
        self._configured = configured
        self.calls = 0

    def is_configured(self) -> bool:
        return self._configured

    async def get_quote(self, food, location) -> ProviderResult:
        self.calls += 1
        return self._result


def fake_chain(
    kroger: ProviderResult | None = None,
    instacart: ProviderResult | None = None,
    bls: ProviderResult | None = None,
    kroger_configured: bool = True,
    instacart_configured: bool = True,
):
    providers = [
        FakeProvider(
            "kroger",
            PriceSource.KROGER_REAL_PRICE,
            kroger or ProviderResult(error="kroger: down"),
            configured=kroger_configured,
        ),
        FakeProvider(
            "instacart",
            PriceSource.INSTACART_NUMERIC_PRICE,
            instacart or ProviderResult(error="instacart: down"),
            configured=instacart_configured,
        ),
        FakeProvider(
            "bls",
            PriceSource.BLS_REGIONAL_AVERAGE,
            bls or ProviderResult(error="bls: no explicit BLS mapping"),
        ),
        SeedProvider(),
    ]
    return providers


async def test_first_provider_wins(foods_by_id):
    providers = fake_chain(kroger=ProviderResult(quote=make_quote(PriceSource.KROGER_REAL_PRICE)))
    engine = PriceEngine(providers)
    quote = await engine.get_price(foods_by_id["eggs_large"], LA)
    assert quote.source is PriceSource.KROGER_REAL_PRICE
    assert quote.provider_error is None
    assert providers[1].calls == 0  # later providers never called


async def test_fallback_order_to_instacart(foods_by_id):
    providers = fake_chain(
        instacart=ProviderResult(quote=make_quote(PriceSource.INSTACART_NUMERIC_PRICE))
    )
    quote = await PriceEngine(providers).get_price(foods_by_id["eggs_large"], LA)
    assert quote.source is PriceSource.INSTACART_NUMERIC_PRICE
    assert "kroger: down" in quote.provider_error


async def test_fallback_order_to_bls(foods_by_id):
    providers = fake_chain(bls=ProviderResult(quote=make_quote(PriceSource.BLS_REGIONAL_AVERAGE)))
    quote = await PriceEngine(providers).get_price(foods_by_id["eggs_large"], LA)
    assert quote.source is PriceSource.BLS_REGIONAL_AVERAGE
    assert "kroger: down" in quote.provider_error
    assert "instacart: down" in quote.provider_error


async def test_fallback_to_seed_when_everything_fails(foods_by_id):
    quote = await PriceEngine(fake_chain()).get_price(foods_by_id["eggs_large"], LA)
    assert quote.source is PriceSource.SEED_ESTIMATE
    assert quote.is_estimate is True
    assert "bls" in quote.provider_error


async def test_confidence_below_threshold_falls_through(foods_by_id):
    providers = fake_chain(
        kroger=ProviderResult(quote=make_quote(PriceSource.KROGER_REAL_PRICE, confidence=0.64))
    )
    quote = await PriceEngine(providers).get_price(foods_by_id["eggs_large"], LA)
    assert quote.source is PriceSource.SEED_ESTIMATE
    assert "below threshold" in quote.provider_error


async def test_confidence_at_threshold_is_accepted(foods_by_id):
    assert MIN_CONFIDENCE == 0.65
    providers = fake_chain(
        kroger=ProviderResult(quote=make_quote(PriceSource.KROGER_REAL_PRICE, confidence=0.65))
    )
    quote = await PriceEngine(providers).get_price(foods_by_id["eggs_large"], LA)
    assert quote.source is PriceSource.KROGER_REAL_PRICE


async def test_unconfigured_provider_skipped_without_call(foods_by_id):
    providers = fake_chain(
        kroger=ProviderResult(quote=make_quote(PriceSource.KROGER_REAL_PRICE)),
        kroger_configured=False,
        instacart=ProviderResult(quote=make_quote(PriceSource.INSTACART_NUMERIC_PRICE)),
    )
    quote = await PriceEngine(providers).get_price(foods_by_id["eggs_large"], LA)
    assert quote.source is PriceSource.INSTACART_NUMERIC_PRICE
    assert providers[0].calls == 0
    assert "kroger: not configured" in quote.provider_error


async def test_provider_exception_does_not_break_chain(foods_by_id):
    class ExplodingProvider(FakeProvider):
        async def get_quote(self, food, location):
            self.calls += 1
            raise RuntimeError("boom")

    providers = [
        ExplodingProvider("kroger", PriceSource.KROGER_REAL_PRICE, ProviderResult()),
        SeedProvider(),
    ]
    quote = await PriceEngine(providers).get_price(foods_by_id["eggs_large"], LA)
    assert quote.source is PriceSource.SEED_ESTIMATE
    assert "RuntimeError: boom" in quote.provider_error


async def test_cache_prevents_second_provider_call(foods_by_id):
    providers = fake_chain(kroger=ProviderResult(quote=make_quote(PriceSource.KROGER_REAL_PRICE)))
    engine = PriceEngine(providers, cache=SessionCache())
    await engine.get_price(foods_by_id["eggs_large"], LA)
    await engine.get_price(foods_by_id["eggs_large"], LA)
    assert providers[0].calls == 1


async def test_cache_also_stores_failures(foods_by_id):
    providers = fake_chain()
    engine = PriceEngine(providers, cache=SessionCache())
    await engine.get_price(foods_by_id["eggs_large"], LA)
    await engine.get_price(foods_by_id["eggs_large"], LA)
    assert providers[0].calls == 1  # failure cached, not retried
    assert providers[1].calls == 1


async def test_cache_distinguishes_locations(foods_by_id):
    providers = fake_chain(kroger=ProviderResult(quote=make_quote(PriceSource.KROGER_REAL_PRICE)))
    engine = PriceEngine(providers, cache=SessionCache())
    await engine.get_price(foods_by_id["eggs_large"], LA)
    await engine.get_price(foods_by_id["eggs_large"], Location(city="New York", zip_code="10001"))
    assert providers[0].calls == 2


async def test_every_quote_carries_source_enum(foods, foods_by_id):
    engine = PriceEngine(fake_chain())
    quotes = await engine.price_all(list(foods)[:5], LA)
    for quote in quotes.values():
        assert isinstance(quote.source, PriceSource)


async def test_price_all_reports_progress(foods):
    subset = list(foods)[:6]
    engine = PriceEngine([SeedProvider()])
    seen = []

    def on_progress(done, total):
        seen.append((done, total))

    quotes = await engine.price_all(subset, LA, on_progress=on_progress)
    assert len(quotes) == 6
    assert seen[-1] == (6, 6)
    assert [d for d, _ in seen] == [1, 2, 3, 4, 5, 6]


async def test_integration_real_providers_without_keys(foods_by_id):
    """Kroger/Instacart unconfigured; BLS prices mapped foods; seed covers the rest."""

    def bls_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "REQUEST_SUCCEEDED",
                "Results": {"series": [{"seriesID": "APU0400708111", "data": [{"value": "4.00"}]}]},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(bls_ok))
    providers = [
        KrogerProvider(None, None, client),
        InstacartProvider(None, client),
        BlsProvider(load_bls_price_map(), client),
        SeedProvider(),
    ]
    engine = PriceEngine(providers)

    eggs_quote = await engine.get_price(foods_by_id["eggs_large"], LA)
    assert eggs_quote.source is PriceSource.BLS_REGIONAL_AVERAGE

    oats_quote = await engine.get_price(foods_by_id["rolled_oats"], LA)
    assert oats_quote.source is PriceSource.SEED_ESTIMATE
    assert "no explicit BLS mapping" in oats_quote.provider_error


async def test_engine_requires_providers():
    with pytest.raises(ValueError):
        PriceEngine([])
