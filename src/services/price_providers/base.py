"""Price provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from models.food import Food
from models.pricing import Location, PriceQuote, PriceSource
from services.cache import CachedEntry

REQUEST_TIMEOUT_SECONDS = 8.0

# Providers return the same shape the cache stores: a quote or an error.
ProviderResult = CachedEntry


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PriceProvider(ABC):
    """One source of prices in the engine's fallback chain."""

    name: str
    source: PriceSource

    @abstractmethod
    async def get_quote(self, food: Food, location: Location) -> ProviderResult:
        """Fetch a quote for one food. Never raises; failures come back as errors."""

    def is_configured(self) -> bool:
        """Whether required credentials are present; the engine skips unconfigured providers."""
        return True

    def result(self, quote: PriceQuote) -> ProviderResult:
        return ProviderResult(quote=quote)

    def failure(self, error: str) -> ProviderResult:
        return ProviderResult(error=f"{self.name}: {error}")
