"""Instacart provider — product matching, availability metadata, numeric prices.

Instacart is used only for product matching, retailer availability metadata,
and numeric price data. A product is usable only when it has BOTH a numeric
price AND a parseable size for a unit price; anything else (price ranges,
"each" with no size, missing fields) falls through to the next provider.

The Instacart Developer Platform API surface varies by partner agreement, so
the endpoint and response parsing are isolated here and kept deliberately
strict; tests exercise this module through a mock transport.
"""

from __future__ import annotations

import httpx

from models.food import Food
from models.pricing import Location, PriceQuote, PriceSource
from services.matching import match_confidence
from services.price_providers.base import (
    REQUEST_TIMEOUT_SECONDS,
    PriceProvider,
    ProviderResult,
    now_iso,
)
from services.units import normalized_price

INSTACART_SEARCH_URL = "https://connect.instacart.com/v2/products/search"


def _numeric_price(value) -> float | None:
    """Accept only plain numeric prices — never ranges or formatted strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


class InstacartProvider(PriceProvider):
    name = "instacart"
    source = PriceSource.INSTACART_NUMERIC_PRICE

    def __init__(self, api_key: str | None, http_client: httpx.AsyncClient):
        self._api_key = api_key
        self._client = http_client

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def get_quote(self, food: Food, location: Location) -> ProviderResult:
        try:
            response = await self._client.get(
                INSTACART_SEARCH_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                params={"query": food.search_terms[0], "postal_code": location.zip_code},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            products = response.json().get("products", [])
        except Exception as exc:  # noqa: BLE001 - providers never raise
            return self.failure(f"{type(exc).__name__}: {exc}")

        best: tuple[float, str, float, str, str, tuple[float, str]] | None = None
        for product in products:
            name = product.get("name", "")
            price = _numeric_price(product.get("price"))
            if price is None:
                continue  # nonnumeric price (range/string) -> not usable
            size = product.get("size", "")
            normalized = normalized_price(price, size, food)
            if normalized is None:
                continue  # no parseable unit price -> not usable
            retailer = product.get("retailer", "Instacart retailer")
            confidence = match_confidence(food.search_terms, name)
            if best is None or confidence > best[0]:
                best = (confidence, name, price, size, retailer, normalized)

        if best is None:
            return self.failure("no product with numeric price and unit price matched")

        confidence, name, price, size, retailer, (per_100, normalized_unit) = best
        return self.result(
            PriceQuote(
                food_name=food.name,
                matched_product_name=name,
                price=price,
                unit=size,
                unit_price=price,
                normalized_unit_price=per_100,
                raw_unit=size,
                normalized_unit=normalized_unit,
                store=retailer,
                source=self.source,
                confidence=confidence,
                is_estimate=False,
                last_updated=now_iso(),
                match_reason=(
                    f"matched '{name}' ({size}) with confidence {confidence:.2f}; "
                    f"available via {retailer}"
                ),
            )
        )
