"""Optional USDA FoodData Central enrichment.

Live FDC lookups are enrichment only: they can display reference data for a
food's fixed ``fdc_id``, but curated seed values always win for planning.
"""

from __future__ import annotations

import httpx

from services.price_providers.base import REQUEST_TIMEOUT_SECONDS

FDC_FOOD_URL = "https://api.nal.usda.gov/fdc/v1/food/{fdc_id}"


async def fetch_food_record(
    fdc_id: int,
    api_key: str,
    http_client: httpx.AsyncClient,
) -> dict | None:
    """Fetch the raw FDC record for a food, or None on any failure."""
    try:
        response = await http_client.get(
            FDC_FOOD_URL.format(fdc_id=fdc_id),
            params={"api_key": api_key},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001 - enrichment is best-effort
        return None
