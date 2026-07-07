"""Session cache behavior tests."""

from models import Location
from services.cache import CachedEntry, SessionCache


def test_keys_distinguish_provider_location_food_and_params():
    la = Location(city="Los Angeles", zip_code="90001")
    ny = Location(city="New York", zip_code="10001")
    keys = {
        SessionCache.make_key("kroger", la, "eggs_large"),
        SessionCache.make_key("bls", la, "eggs_large"),
        SessionCache.make_key("kroger", ny, "eggs_large"),
        SessionCache.make_key("kroger", la, "milk_whole"),
        SessionCache.make_key("kroger", la, "eggs_large", {"limit": 5}),
    }
    assert len(keys) == 5


def test_key_params_order_independent():
    la = Location(city="Los Angeles", zip_code="90001")
    a = SessionCache.make_key("kroger", la, "eggs_large", {"a": 1, "b": 2})
    b = SessionCache.make_key("kroger", la, "eggs_large", {"b": 2, "a": 1})
    assert a == b


def test_get_put_and_stats():
    cache = SessionCache()
    la = Location(city="Los Angeles", zip_code="90001")
    key = SessionCache.make_key("seed", la, "eggs_large")
    assert cache.get(key) is None
    assert cache.misses == 1
    cache.put(key, CachedEntry(error="seed: boom"))
    entry = cache.get(key)
    assert entry is not None
    assert entry.error == "seed: boom"
    assert cache.hits == 1
    assert len(cache) == 1


def test_failures_are_cached_too():
    cache = SessionCache()
    la = Location(city="Los Angeles", zip_code="90001")
    key = SessionCache.make_key("kroger", la, "oats")
    cache.put(key, CachedEntry(quote=None, error="kroger: timeout"))
    entry = cache.get(key)
    assert entry is not None and entry.quote is None and "timeout" in entry.error
