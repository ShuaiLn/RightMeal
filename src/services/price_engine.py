"""Price lookup and deferred package-offer fallback orchestration.

The legacy ``get_price`` / ``price_all`` API remains available for current UI
callers.  New planning code uses ``get_package_offers`` / ``price_all_offers``:
live retailer offers form the first tier, BLS the second, and local seed offers
are enabled only for explicitly required missing foods.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Awaitable, Callable, Iterable, Sequence

from models.food import Food
from models.pricing import (
    Location,
    OfferBook,
    OfferLookup,
    PackageOffer,
    PriceLookupDiagnostics,
    PriceQuote,
    PriceSource,
    dollars_to_cents,
    seed_package_offers,
)
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

    async def _provider_result(
        self, provider: PriceProvider, food: Food, location: Location
    ) -> ProviderResult:
        key = SessionCache.make_key(provider.name, location, food.id)
        result = self.cache.get(key)
        if result is None:
            try:
                result = await provider.get_quote(food, location)
            except Exception as exc:  # noqa: BLE001 - keep the chain alive
                result = ProviderResult(
                    error=f"{provider.name}: {type(exc).__name__}: {exc}"
                )
            self.cache.put(key, result)
        return result

    async def get_price(self, food: Food, location: Location) -> PriceQuote:
        """Legacy first-usable-quote API, including terminal seed fallback."""

        failures: list[str] = []
        for provider in self.providers:
            if not provider.is_configured():
                failures.append(f"{provider.name}: not configured")
                continue
            result = await self._provider_result(provider, food, location)
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
            f"No provider produced a quote for {food.id!r}; the seed provider "
            "must always be configured last"
        )

    @staticmethod
    def _provider_tier(provider: PriceProvider) -> int:
        if provider.source in (
            PriceSource.KROGER_REAL_PRICE,
            PriceSource.INSTACART_NUMERIC_PRICE,
        ):
            return 0
        if provider.source is PriceSource.BLS_REGIONAL_AVERAGE:
            return 1
        return 2

    @staticmethod
    def _bls_package_offers(food: Food, quote: PriceQuote) -> tuple[PackageOffer, ...]:
        """Project a normalized regional estimate onto each catalog package.

        BLS is a unit-price estimate rather than one observed retailer SKU, so
        every positive catalog package can receive its own integer-cent offer.
        Live retailer quotes remain bound to their one observed package.
        """

        offers: list[PackageOffer] = []
        for package in food.package_options:
            basis = (
                package.ml
                if food.is_liquid and quote.normalized_unit == "100ml"
                else package.grams
            )
            if basis is None or basis <= 0:
                continue
            cents = dollars_to_cents(quote.normalized_unit_price * basis / 100.0)
            if cents <= 0:
                continue
            offers.append(PackageOffer.for_catalog_package(
                food,
                package,
                price_cents=cents,
                source=quote.source,
                store=quote.store,
                matched_product_name=quote.matched_product_name,
                confidence=quote.confidence,
                is_estimate=True,
                last_updated=quote.last_updated,
                match_reason=quote.match_reason,
                raw_unit=package.label,
                provider_error=quote.provider_error,
            ))
        return tuple(offers)

    async def get_package_offers(
        self,
        food: Food,
        location: Location,
        *,
        allow_local_fallback: bool = False,
    ) -> OfferLookup:
        """Collect the best available tier of package-specific offers.

        All live providers may contribute to the live tier. BLS is consulted
        only when live has no usable offer. Seed is consulted only when the
        caller marks this missing food as blocking planning. A fallback request
        therefore never overwrites a usable live or BLS offer.
        """

        failures: list[str] = []
        for tier in (0, 1, 2):
            if tier == 2 and not allow_local_fallback:
                break
            tier_offers: list[PackageOffer] = []
            for provider in self.providers:
                if self._provider_tier(provider) != tier:
                    continue
                if not provider.is_configured():
                    failures.append(f"{provider.name}: not configured")
                    continue
                result = await self._provider_result(provider, food, location)
                if result.quote is None:
                    failures.append(result.error or f"{provider.name}: no quote")
                    continue
                quote = result.quote
                if quote.confidence < MIN_CONFIDENCE:
                    failures.append(
                        f"{provider.name}: confidence {quote.confidence:.2f} "
                        f"below threshold {MIN_CONFIDENCE}"
                    )
                    continue
                try:
                    if quote.source is PriceSource.SEED_ESTIMATE:
                        offers = seed_package_offers(
                            food, last_updated=quote.last_updated
                        )
                    elif quote.source is PriceSource.BLS_REGIONAL_AVERAGE:
                        offers = self._bls_package_offers(food, quote)
                    else:
                        offers = (PackageOffer.from_quote(food, quote),)
                except ValueError as exc:
                    failures.append(f"{provider.name}: unusable package offer: {exc}")
                    continue
                provider_error = "; ".join(failures) if failures else None
                tier_offers.extend(
                    replace(offer, provider_error=provider_error) for offer in offers
                )
            if tier_offers:
                by_triple = {
                    (offer.food_id, offer.package_id, offer.offer_id): offer
                    for offer in tier_offers
                }
                ordered = tuple(
                    sorted(by_triple.values(), key=lambda o: (o.offer_id, o.package_id))
                )
                fallback = tier == 2
                return OfferLookup(
                    offers=ordered,
                    diagnostics=PriceLookupDiagnostics(
                        provider_failures=tuple(failures),
                        local_fallback_used=fallback,
                        fallback_food_ids=((food.id,) if fallback else ()),
                        fallback_sources=(
                            (PriceSource.SEED_ESTIMATE,) if fallback else ()
                        ),
                    ),
                )
        return OfferLookup(
            offers=(),
            diagnostics=PriceLookupDiagnostics(provider_failures=tuple(failures)),
        )

    async def price_all_offers(
        self,
        foods: Sequence[Food],
        location: Location,
        *,
        local_fallback_food_ids: Iterable[str] = (),
        on_progress: ProgressCallback | None = None,
    ) -> OfferBook:
        """Price foods with seed fallback deferred to explicitly required ids."""

        fallback_ids = frozenset(local_fallback_food_ids)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_LOOKUPS)
        done = 0
        total = len(foods)
        lock = asyncio.Lock()

        async def fetch(food: Food) -> tuple[str, OfferLookup]:
            nonlocal done
            async with semaphore:
                lookup = await self.get_package_offers(
                    food,
                    location,
                    allow_local_fallback=food.id in fallback_ids,
                )
            async with lock:
                done += 1
                if on_progress is not None:
                    maybe_coro = on_progress(done, total)
                    if maybe_coro is not None:
                        await maybe_coro
            return food.id, lookup

        pairs = await asyncio.gather(*(fetch(food) for food in foods))
        offers_by_food = {
            food_id: lookup.offers
            for food_id, lookup in pairs
            if lookup.offers
        }
        missing = tuple(sorted(
            food_id for food_id, lookup in pairs if not lookup.offers
        ))
        fallback_used_ids = tuple(sorted(
            food_id
            for food_id, lookup in pairs
            if lookup.diagnostics.local_fallback_used
        ))
        failures = tuple(
            f"{food_id}: {failure}"
            for food_id, lookup in pairs
            for failure in lookup.diagnostics.provider_failures
        )
        return OfferBook(
            offers_by_food=offers_by_food,
            missing_food_ids=missing,
            diagnostics=PriceLookupDiagnostics(
                provider_failures=failures,
                local_fallback_used=bool(fallback_used_ids),
                fallback_food_ids=fallback_used_ids,
                fallback_sources=(
                    (PriceSource.SEED_ESTIMATE,) if fallback_used_ids else ()
                ),
            ),
        )

    async def price_all(
        self,
        foods: Sequence[Food],
        location: Location,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, PriceQuote]:
        """Legacy quote API used by the current Start view."""

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
