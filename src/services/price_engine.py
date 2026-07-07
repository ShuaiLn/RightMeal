"""Price engine: orchestrates the provider fallback chain with caching.

Priority: Kroger real price -> Instacart (numeric price + unit price only)
-> BLS regional average (explicitly mapped foods only) -> seed estimate.
Quotes below the confidence threshold fall through to the next provider.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Awaitable, Callable, Sequence

from models.food import Food
from models.pricing import Location, PriceQuote
from services.cache import SessionCache
from services.price_providers.base import PriceProvider, ProviderResult

MIN_CONFIDENCE = 0.65
MAX_CONCURRENT_LOOKUPS = 4

ProgressCallback = Callable[[int, int], Awaitable[None] | None]


class PriceEngine:
    def __init__(self, providers: Sequence[PriceProvider], cache: SessionCache | None = None):
        if not providers:
            raise ValueError("PriceEngine needs at least one provider")
        self.providers = list(providers)
        self.cache = cache if cache is not None else SessionCache()

    async def get_price(self, food: Food, location: Location) -> PriceQuote:
        """Return the best available quote, annotated with earlier failures."""
        failures: list[str] = []
        for provider in self.providers:
            if not provider.is_configured():
                failures.append(f"{provider.name}: not configured")
                continue

            key = SessionCache.make_key(provider.name, location, food.id)
            result = self.cache.get(key)
            if result is None:
                try:
                    result = await provider.get_quote(food, location)
                except Exception as exc:  # noqa: BLE001 - keep the chain alive
                    result = ProviderResult(error=f"{provider.name}: {type(exc).__name__}: {exc}")
                self.cache.put(key, result)

            if result.quote is None:
                failures.append(result.error or f"{provider.name}: no quote")
                continue
            if result.quote.confidence < MIN_CONFIDENCE:
                failures.append(
                    f"{provider.name}: confidence {result.quote.confidence:.2f} "
                    f"below threshold {MIN_CONFIDENCE}"
                )
                continue

            provider_error = "; ".join(failures) if failures else None
            return replace(result.quote, provider_error=provider_error)

        raise RuntimeError(
            f"No provider produced a quote for {food.id!r} — the seed provider "
            "must always be configured last"
        )

    async def price_all(
        self,
        foods: Sequence[Food],
        location: Location,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, PriceQuote]:
        """Price all foods concurrently (bounded) with optional progress reporting."""
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_LOOKUPS)
        done = 0
        total = len(foods)
        lock = asyncio.Lock()

        async def fetch(food: Food) -> tuple[str, PriceQuote]:
            nonlocal done
            async with semaphore:
                quote = await self.get_price(food, location)
            async with lock:
                done += 1
                if on_progress is not None:
                    maybe_coro = on_progress(done, total)
                    if maybe_coro is not None:
                        await maybe_coro
            return food.id, quote

        pairs = await asyncio.gather(*(fetch(food) for food in foods))
        return dict(pairs)
