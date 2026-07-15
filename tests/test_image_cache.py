"""Image cache tests with a mocked HTTP transport (no real network)."""

import hashlib

import httpx
import pytest

from services.image_cache import ImageCache

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-data"
GOOD_URL = "https://img.example.com/eggs.png"
MISSING_URL = "https://img.example.com/nope.png"
HTML_URL = "https://img.example.com/page.html"


@pytest.fixture
def transport_calls():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == GOOD_URL:
            return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
        if str(request.url) == HTML_URL:
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


async def test_fetch_caches_to_sha1_file(tmp_path, transport_calls):
    transport, calls = transport_calls
    async with httpx.AsyncClient(transport=transport) as client:
        cache = ImageCache(tmp_path, client)
        data = await cache.fetch(GOOD_URL)
        assert data == PNG_BYTES
        expected = tmp_path / (hashlib.sha1(GOOD_URL.encode()).hexdigest() + ".img")
        assert expected.read_bytes() == PNG_BYTES
        assert cache.get_cached(GOOD_URL) == PNG_BYTES


async def test_second_fetch_hits_disk_not_network(tmp_path, transport_calls):
    transport, calls = transport_calls
    async with httpx.AsyncClient(transport=transport) as client:
        cache = ImageCache(tmp_path, client)
        await cache.fetch(GOOD_URL)
        assert len(calls) == 1
        await cache.fetch(GOOD_URL)
        assert len(calls) == 1  # served from disk
        # a fresh cache instance also reads from disk without network
        cache2 = ImageCache(tmp_path, client)
        assert await cache2.fetch(GOOD_URL) == PNG_BYTES
        assert len(calls) == 1


async def test_404_and_non_image_return_none_without_file(tmp_path, transport_calls):
    transport, calls = transport_calls
    async with httpx.AsyncClient(transport=transport) as client:
        cache = ImageCache(tmp_path, client)
        assert await cache.fetch(MISSING_URL) is None
        assert await cache.fetch(HTML_URL) is None
        assert list(tmp_path.glob("*.img")) == []
        # failures are memoized for the session
        assert await cache.fetch(MISSING_URL) is None
        assert calls.count(MISSING_URL) == 1


async def test_prefetch_swallows_failures(tmp_path, transport_calls):
    transport, _ = transport_calls
    async with httpx.AsyncClient(transport=transport) as client:
        cache = ImageCache(tmp_path, client)
        await cache.prefetch([GOOD_URL, MISSING_URL, HTML_URL, "", GOOD_URL])
        assert cache.get_cached(GOOD_URL) == PNG_BYTES
        assert cache.get_cached(MISSING_URL) is None


def test_get_cached_never_touches_network(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("get_cached must not hit the network")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ImageCache(tmp_path, client)
    assert cache.get_cached(GOOD_URL) is None
    assert cache.get_cached(None) is None
    assert cache.get_cached("") is None
