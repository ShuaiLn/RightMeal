"""Curated seed price estimates — the always-available terminal fallback."""

from __future__ import annotations

from models.food import Food
from models.pricing import Location, PriceQuote, PriceSource
from services.price_providers.base import PriceProvider, ProviderResult, now_iso


class SeedProvider(PriceProvider):
    name = "seed"
    source = PriceSource.SEED_ESTIMATE

    async def get_quote(self, food: Food, location: Location) -> ProviderResult:
        # Representative package: the best value per 100 g / 100 ml.
        pkg = min(food.package_options, key=lambda p: (food.seed_cost_per_100(p), p.label))
        return self.result(
            PriceQuote(
                food_name=food.name,
                matched_product_name=food.name,
                price=pkg.seed_price,
                unit=pkg.label,
                unit_price=pkg.seed_price,
                normalized_unit_price=food.seed_cost_per_100(pkg),
                raw_unit=pkg.label,
                normalized_unit="100ml" if food.is_liquid else "100g",
                store="Seed data",
                source=self.source,
                confidence=1.0,
                is_estimate=True,
                last_updated=now_iso(),
                match_reason="curated seed estimate",
            )
        )
