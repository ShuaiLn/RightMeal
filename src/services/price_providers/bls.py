"""BLS Average Price (AP) survey provider.

Only foods with an explicit mapping in ``bls_price_map.json`` may be priced
here; anything else immediately falls through to the next provider. Works
without an API key (lower rate limits apply).
"""

from __future__ import annotations

import httpx

from models.food import Food
from models.pricing import Location, PriceQuote, PriceSource
from services.price_providers.base import (
    REQUEST_TIMEOUT_SECONDS,
    PriceProvider,
    ProviderResult,
    now_iso,
)

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


class BlsProvider(PriceProvider):
    name = "bls"
    source = PriceSource.BLS_REGIONAL_AVERAGE

    def __init__(
        self,
        bls_map: dict,
        http_client: httpx.AsyncClient,
        api_key: str | None = None,
    ):
        self._map = bls_map
        self._client = http_client
        self._api_key = api_key

    def _area_code(self, location: Location) -> str:
        prefixes = self._map["area_codes"]["zip_prefix_to_area"]
        default = self._map["area_codes"]["default"]
        if location.zip_code:
            return prefixes.get(location.zip_code[0], default)
        return default

    async def get_quote(self, food: Food, location: Location) -> ProviderResult:
        series = self._map.get("series", {}).get(food.id)
        if series is None:
            return self.failure("no explicit BLS mapping for this food")

        area = self._area_code(location)
        default_area = self._map["area_codes"]["default"]
        regional_id = f"APU{area}{series['item_code']}"
        national_id = f"APU{default_area}{series['item_code']}"
        series_ids = [regional_id] if regional_id == national_id else [regional_id, national_id]

        payload: dict = {"seriesid": series_ids, "latest": True}
        if self._api_key:
            payload["registrationkey"] = self._api_key

        try:
            response = await self._client.post(
                BLS_API_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - providers never raise
            return self.failure(f"{type(exc).__name__}: {exc}")

        if body.get("status") != "REQUEST_SUCCEEDED":
            return self.failure(f"BLS request failed: {body.get('message') or body.get('status')}")

        values: dict[str, float] = {}
        for entry in body.get("Results", {}).get("series", []):
            data = entry.get("data") or []
            if not data:
                continue
            try:
                values[entry["seriesID"]] = float(data[0]["value"])
            except (KeyError, TypeError, ValueError):
                continue

        used_id = regional_id if regional_id in values else national_id
        if used_id not in values:
            return self.failure(f"no data for series {regional_id}")
        price = values[used_id]
        scope = "regional" if used_id == regional_id else "U.S. average"

        if food.is_liquid:
            ml_per_unit = series.get("ml_per_unit")
            if not ml_per_unit:
                return self.failure("liquid food mapping is missing ml_per_unit")
            normalized = price / (float(ml_per_unit) / 100.0)
            normalized_unit = "100ml"
        else:
            normalized = price / (float(series["grams_per_unit"]) / 100.0)
            normalized_unit = "100g"

        description = series.get("description", food.name)
        return self.result(
            PriceQuote(
                food_name=food.name,
                matched_product_name=description,
                price=price,
                unit=series["bls_unit"],
                unit_price=price,
                normalized_unit_price=normalized,
                raw_unit=series["bls_unit"],
                normalized_unit=normalized_unit,
                store="U.S. BLS region average",
                source=self.source,
                confidence=0.9,
                is_estimate=True,
                last_updated=now_iso(),
                match_reason=f"BLS series {used_id} ({scope}): {description}",
            )
        )
