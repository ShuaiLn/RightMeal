"""Local disk cache for food photos.

Images download once (validated as actual images) and then serve from disk
forever, so the app works offline after the first plan and never blocks the
UI on the network for rendering.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

import httpx

MAX_IMAGE_BYTES = 5 * 1024 * 1024


class ImageCache:
    def __init__(self, cache_dir: Path, client: httpx.AsyncClient):
        self.cache_dir = Path(cache_dir)
        self._client = client
        self._failed: set[str] = set()  # per-session memo, avoids re-hitting dead URLs

    def _path_for(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")

    def get_cached(self, url: str | None) -> bytes | None:
        """Cached bytes from disk, or None. Never touches the network."""
        if not url:
            return None
        try:
            return self._path_for(url).read_bytes()
        except OSError:
            return None

    async def fetch(self, url: str) -> bytes | None:
        """Return image bytes, downloading and caching on first use."""
        cached = self.get_cached(url)
        if cached is not None:
            return cached
        if url in self._failed:
            return None
        try:
            response = await self._client.get(url, follow_redirects=True, timeout=10.0)
            content_type = response.headers.get("content-type", "")
            if (
                response.status_code != 200
                or not content_type.startswith("image/")
                or len(response.content) > MAX_IMAGE_BYTES
                or not response.content
            ):
                self._failed.add(url)
                return None
            self._write_atomic(self._path_for(url), response.content)
            return response.content
        except (httpx.HTTPError, OSError):
            self._failed.add(url)
            return None

    async def prefetch(self, urls: list[str], concurrency: int = 4) -> None:
        """Warm the cache for many URLs; failures are silently skipped."""
        unique = sorted({url for url in urls if url})
        semaphore = asyncio.Semaphore(concurrency)

        async def worker(url: str) -> None:
            async with semaphore:
                await self.fetch(url)

        await asyncio.gather(*(worker(url) for url in unique))

    def _write_atomic(self, path: Path, data: bytes) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
