"""Session-scoped cache for external price lookups.

Caches both successful quotes and failures so a provider that already failed
for a food is not retried within the same session (reduces latency, repeated
calls, and rate-limit risk during demos).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from models.pricing import Location, PriceQuote


@dataclass(frozen=True)
class CachedEntry:
    quote: PriceQuote | None = None
    error: str | None = None


class SessionCache:
    def __init__(self) -> None:
        self._store: dict[str, CachedEntry] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(provider: str, location: Location, food_id: str, params: dict | None = None) -> str:
        param_part = json.dumps(params or {}, sort_keys=True)
        return f"{provider}|{location.zip_code}|{location.city.lower()}|{food_id}|{param_part}"

    def get(self, key: str) -> CachedEntry | None:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
        else:
            self.hits += 1
        return entry

    def put(self, key: str, entry: CachedEntry) -> None:
        self._store[key] = entry

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
