"""Per-provider tests via httpx.MockTransport — no network access."""

import httpx
import pytest

from data import load_bls_price_map
from models import Location, PriceSource
from services.price_providers import (
    BlsProvider,
    InstacartProvider,
    KrogerProvider,
    SeedProvider,
)

LA = Location(city="Los Angeles", zip_code="90001")


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- Seed ---------------------------------------------------------------


async def test_seed_always_succeeds(foods_by_id):
    provider = SeedProvider()
    result = await provider.get_quote(foods_by_id["eggs_large"], LA)
    quote = result.quote
    assert quote is not None
    assert quote.source is PriceSource.SEED_ESTIMATE
    assert quote.is_estimate is True
    assert quote.confidence == 1.0
    assert quote.store == "Seed data"


async def test_seed_picks_best_value_package(foods_by_id):
    rice = foods_by_id["rice_white"]  # 1 lb $0.99 vs 5 lb $3.99 (better per gram)
    result = await provider_quote(SeedProvider(), rice)
    assert result.unit == "5 lb bag"
    assert result.normalized_unit == "100g"
    assert result.normalized_unit_price == pytest.approx(3.99 / 22.68, rel=1e-3)


async def test_seed_liquid_normalizes_per_100ml(foods_by_id):
    milk = foods_by_id["milk_whole"]
    quote = await provider_quote(SeedProvider(), milk)
    assert quote.normalized_unit == "100ml"


async def provider_quote(provider, food, location=LA):
    result = await provider.get_quote(food, location)
    assert result.quote is not None, result.error
    return result.quote


# --- Kroger --------------------------------------------------------------


def kroger_handler(products: dict, *, fail: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2/token" in url:
            if fail == "token":
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        if "/v1/locations" in url:
            return httpx.Response(
                200,
                json={"data": [{"locationId": "70300123", "chain": "ralphs", "name": "Downtown LA"}]},
            )
        if "/v1/products" in url:
            if fail == "timeout":
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(200, json=products)
        return httpx.Response(404)

    return handler


EGG_PRODUCTS = {
    "data": [
        {
            "description": "Kroger Grade A Large Eggs",
            "items": [{"price": {"regular": 3.49}, "size": "12 ct"}],
        },
        {
            "description": "Simple Truth Organic Eggs",
            "items": [{"price": {"regular": 6.99}, "size": "12 ct"}],
        },
    ]
}


async def test_kroger_unconfigured_without_keys():
    provider = KrogerProvider(None, None, client_with(kroger_handler({})))
    assert provider.is_configured() is False


async def test_kroger_happy_path(foods_by_id):
    provider = KrogerProvider("id", "secret", client_with(kroger_handler(EGG_PRODUCTS)))
    quote = await provider_quote(provider, foods_by_id["eggs_large"])
    assert quote.source is PriceSource.KROGER_REAL_PRICE
    assert quote.is_estimate is False
    assert quote.price == 3.49
    assert quote.store == "Ralphs Downtown LA"
    assert quote.matched_product_name == "Kroger Grade A Large Eggs"
    # 12 ct -> 600 g, so $3.49 -> ~$0.58 per 100 g
    assert quote.normalized_unit_price == pytest.approx(3.49 / 6.0)
    assert quote.confidence >= 0.65


async def test_kroger_promo_price_wins(foods_by_id):
    products = {
        "data": [
            {
                "description": "Kroger Grade A Large Eggs",
                "items": [{"price": {"regular": 3.49, "promo": 2.99}, "size": "12 ct"}],
            }
        ]
    }
    provider = KrogerProvider("id", "secret", client_with(kroger_handler(products)))
    quote = await provider_quote(provider, foods_by_id["eggs_large"])
    assert quote.price == 2.99


async def test_kroger_timeout_is_recorded_not_raised(foods_by_id):
    provider = KrogerProvider("id", "secret", client_with(kroger_handler({}, fail="timeout")))
    result = await provider.get_quote(foods_by_id["eggs_large"], LA)
    assert result.quote is None
    assert "ReadTimeout" in result.error


async def test_kroger_auth_failure_is_recorded(foods_by_id):
    provider = KrogerProvider("id", "bad", client_with(kroger_handler({}, fail="token")))
    result = await provider.get_quote(foods_by_id["eggs_large"], LA)
    assert result.quote is None
    assert result.error.startswith("kroger:")


async def test_kroger_rejects_products_without_usable_price_or_size(foods_by_id):
    products = {
        "data": [
            {"description": "Large Eggs", "items": [{"price": {}, "size": "12 ct"}]},
            {"description": "Large Eggs", "items": [{"price": {"regular": "3.49"}, "size": "12 ct"}]},
            {"description": "Large Eggs", "items": [{"price": {"regular": 3.49}, "size": "fresh"}]},
        ]
    }
    provider = KrogerProvider("id", "secret", client_with(kroger_handler(products)))
    result = await provider.get_quote(foods_by_id["eggs_large"], LA)
    assert result.quote is None
    assert "no product with usable price" in result.error


# --- Instacart -----------------------------------------------------------


def instacart_handler(products: list):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"products": products})

    return handler


async def test_instacart_unconfigured_without_key():
    provider = InstacartProvider(None, client_with(instacart_handler([])))
    assert provider.is_configured() is False


async def test_instacart_happy_path_numeric_price_and_size(foods_by_id):
    products = [
        {"name": "Large Eggs, Grade A", "price": 3.79, "size": "12 ct", "retailer": "Vons"},
    ]
    provider = InstacartProvider("key", client_with(instacart_handler(products)))
    quote = await provider_quote(provider, foods_by_id["eggs_large"])
    assert quote.source is PriceSource.INSTACART_NUMERIC_PRICE
    assert quote.price == 3.79
    assert quote.store == "Vons"
    assert "available via Vons" in quote.match_reason


@pytest.mark.parametrize(
    "product",
    [
        {"name": "Large Eggs", "price": "$3.79", "size": "12 ct"},  # string price
        {"name": "Large Eggs", "price": "2.99-3.99", "size": "12 ct"},  # range
        {"name": "Large Eggs", "price": 3.79},  # no size -> no unit price
        {"name": "Large Eggs", "price": 3.79, "size": "each"},  # unparseable size
        {"name": "Large Eggs", "price": True, "size": "12 ct"},  # bool is not a price
    ],
)
async def test_instacart_rejects_without_numeric_price_and_unit_price(foods_by_id, product):
    provider = InstacartProvider("key", client_with(instacart_handler([product])))
    result = await provider.get_quote(foods_by_id["eggs_large"], LA)
    assert result.quote is None
    assert "numeric price and unit price" in result.error


async def test_instacart_http_error_recorded(foods_by_id):
    def handler(request):
        return httpx.Response(500, json={"error": "server"})

    provider = InstacartProvider("key", client_with(handler))
    result = await provider.get_quote(foods_by_id["eggs_large"], LA)
    assert result.quote is None
    assert result.error.startswith("instacart:")


# --- BLS -----------------------------------------------------------------


def bls_handler(series_values: dict[str, str], status: str = "REQUEST_SUCCEEDED"):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        series = [
            {"seriesID": sid, "data": [{"value": value}] if value is not None else []}
            for sid, value in series_values.items()
        ]
        return httpx.Response(200, json={"status": status, "Results": {"series": series}})

    return handler, calls


@pytest.fixture(scope="module")
def bls_map():
    return load_bls_price_map()


async def test_bls_skips_unmapped_foods_without_any_request(foods_by_id, bls_map):
    handler, calls = bls_handler({})
    provider = BlsProvider(bls_map, client_with(handler))
    for food_id in ("rolled_oats", "lentils_dry", "broccoli_frozen", "mixed_veg_frozen"):
        result = await provider.get_quote(foods_by_id[food_id], LA)
        assert result.quote is None
        assert "no explicit BLS mapping" in result.error
    assert calls == []  # never touched the network


async def test_bls_regional_series_for_west_zip(foods_by_id, bls_map):
    handler, calls = bls_handler({"APU0400708111": "4.12", "APU0000708111": "3.80"})
    provider = BlsProvider(bls_map, client_with(handler))
    quote = await provider_quote(provider, foods_by_id["eggs_large"])
    assert quote.source is PriceSource.BLS_REGIONAL_AVERAGE
    assert quote.price == pytest.approx(4.12)  # regional (West, ZIP 9xxxx) wins
    assert quote.is_estimate is True
    assert quote.confidence == 0.9
    # $4.12 per dozen (600 g) -> ~$0.687 per 100 g
    assert quote.normalized_unit_price == pytest.approx(4.12 / 6.0)
    assert "APU0400708111" in quote.match_reason


async def test_bls_falls_back_to_national_series(foods_by_id, bls_map):
    handler, _ = bls_handler({"APU0400708111": None, "APU0000708111": "3.80"})
    provider = BlsProvider(bls_map, client_with(handler))
    quote = await provider_quote(provider, foods_by_id["eggs_large"])
    assert quote.price == pytest.approx(3.80)
    assert "U.S. average" in quote.match_reason


async def test_bls_liquid_normalizes_per_100ml(foods_by_id, bls_map):
    handler, _ = bls_handler({"APU0400709112": "3.90", "APU0000709112": "3.99"})
    provider = BlsProvider(bls_map, client_with(handler))
    quote = await provider_quote(provider, foods_by_id["milk_whole"])
    assert quote.normalized_unit == "100ml"
    assert quote.normalized_unit_price == pytest.approx(3.90 / 37.85)


async def test_bls_request_failure_recorded(foods_by_id, bls_map):
    handler, _ = bls_handler({}, status="REQUEST_NOT_PROCESSED")
    provider = BlsProvider(bls_map, client_with(handler))
    result = await provider.get_quote(foods_by_id["eggs_large"], LA)
    assert result.quote is None
    assert "BLS request failed" in result.error


async def test_bls_is_always_configured(bls_map):
    handler, _ = bls_handler({})
    assert BlsProvider(bls_map, client_with(handler)).is_configured() is True
