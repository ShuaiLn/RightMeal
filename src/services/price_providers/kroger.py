"""Kroger Products API provider — real store prices near a ZIP code."""

from __future__ import annotations

import base64
import time

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

KROGER_BASE_URL = "https://api.kroger.com"
TOKEN_URL = f"{KROGER_BASE_URL}/v1/connect/oauth2/token"
LOCATIONS_URL = f"{KROGER_BASE_URL}/v1/locations"
PRODUCTS_URL = f"{KROGER_BASE_URL}/v1/products"


class KrogerProvider(PriceProvider):
    name = "kroger"
    source = PriceSource.KROGER_REAL_PRICE

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        http_client: httpx.AsyncClient,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = http_client
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._location_cache: dict[str, tuple[str, str] | None] = {}  # zip -> (id, name)

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        credentials = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        response = await self._client.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": "product.compact"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + float(body.get("expires_in", 1800)) - 60
        return self._token

    async def _find_location(self, zip_code: str, token: str) -> tuple[str, str] | None:
        if zip_code in self._location_cache:
            return self._location_cache[zip_code]
        response = await self._client.get(
            LOCATIONS_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"filter.zipCode.near": zip_code, "filter.limit": 1},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        result = None
        if data:
            store = data[0]
            chain = store.get("chain", "Kroger").title()
            name = store.get("name", "")
            result = (store["locationId"], f"{chain} {name}".strip())
        self._location_cache[zip_code] = result
        return result

    async def get_quote(self, food: Food, location: Location) -> ProviderResult:
        try:
            token = await self._get_token()
            store = await self._find_location(location.zip_code, token)
            if store is None:
                return self.failure(f"no store found near ZIP {location.zip_code}")
            location_id, store_name = store

            response = await self._client.get(
                PRODUCTS_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "filter.term": food.search_terms[0],
                    "filter.locationId": location_id,
                    "filter.limit": 10,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            products = response.json().get("data", [])
        except Exception as exc:  # noqa: BLE001 - providers never raise
            return self.failure(f"{type(exc).__name__}: {exc}")

        best: tuple[float, dict, float, str, tuple[float, str]] | None = None
        for product in products:
            description = product.get("description", "")
            items = product.get("items") or []
            if not items:
                continue
            price_info = items[0].get("price") or {}
            price = price_info.get("promo") or price_info.get("regular")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            size = items[0].get("size", "")
            normalized = normalized_price(float(price), size, food)
            if normalized is None:
                continue
            confidence = match_confidence(food.search_terms, description)
            if best is None or confidence > best[0]:
                best = (confidence, product, float(price), size, normalized)

        if best is None:
            return self.failure("no product with usable price and size matched")

        confidence, product, price, size, (per_100, normalized_unit) = best
        description = product.get("description", "")
        return self.result(
            PriceQuote(
                food_name=food.name,
                matched_product_name=description,
                price=price,
                unit=size,
                unit_price=price,
                normalized_unit_price=per_100,
                raw_unit=size,
                normalized_unit=normalized_unit,
                store=store_name,
                source=self.source,
                confidence=confidence,
                is_estimate=False,
                last_updated=now_iso(),
                match_reason=f"matched '{description}' ({size}) with confidence {confidence:.2f}",
            )
        )
