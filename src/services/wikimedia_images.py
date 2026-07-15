"""Licensed Custom Pantry image search through the Wikimedia Commons API."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import re
from urllib.parse import quote

import httpx

from services.photo_images import NormalizedImage, normalize_image

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MAX_IMAGE_SIDE = 10_000


@dataclass(frozen=True)
class WikimediaImageResult:
    title: str
    image_url: str
    thumbnail_url: str
    source_page: str
    mime: str
    width: int
    height: int
    author: str
    license_name: str
    license_url: str | None


def _metadata_value(metadata: dict, key: str) -> str:
    value = metadata.get(key, {})
    if not isinstance(value, dict):
        return ""
    raw = unescape(str(value.get("value", "")))
    return re.sub(r"<[^>]+>", "", raw).strip()


class WikimediaImageSearch:
    def __init__(self, http_client: httpx.AsyncClient):
        self._client = http_client

    async def search(self, query: str, limit: int = 8) -> tuple[WikimediaImageResult, ...]:
        query = query.strip()
        if not query:
            return ()
        response = await self._client.get(
            COMMONS_API_URL,
            params={
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {query}",
                "gsrnamespace": "6",
                "gsrlimit": str(max(1, min(limit, 20))),
                "prop": "imageinfo",
                "iiprop": "url|mime|size|extmetadata",
                "iiurlwidth": "480",
                "origin": "*",
            },
            timeout=20.0,
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", [])
        results: list[WikimediaImageResult] = []
        for page in pages:
            info_values = page.get("imageinfo") or []
            if not info_values:
                continue
            info = info_values[0]
            image_url = str(info.get("url", ""))
            thumbnail_url = str(info.get("thumburl") or image_url)
            mime = str(info.get("mime", ""))
            width = int(info.get("width", 0) or 0)
            height = int(info.get("height", 0) or 0)
            if (
                not image_url.startswith("https://")
                or not thumbnail_url.startswith("https://")
                or not mime.startswith("image/")
                or width <= 0
                or height <= 0
                or width > MAX_IMAGE_SIDE
                or height > MAX_IMAGE_SIDE
            ):
                continue
            title = str(page.get("title", ""))
            metadata = info.get("extmetadata") or {}
            license_name = _metadata_value(metadata, "LicenseShortName")
            author = _metadata_value(metadata, "Artist")
            license_url = _metadata_value(metadata, "LicenseUrl") or None
            if not license_name or not author:
                continue
            results.append(WikimediaImageResult(
                title=title.removeprefix("File:"),
                image_url=image_url,
                thumbnail_url=thumbnail_url,
                source_page=f"https://commons.wikimedia.org/wiki/{quote(title.replace(' ', '_'))}",
                mime=mime,
                width=width,
                height=height,
                author=author,
                license_name=license_name,
                license_url=license_url,
            ))
        return tuple(results)

    async def download(self, result: WikimediaImageResult) -> NormalizedImage:
        """Download and validate the user-selected result before local caching."""

        if not result.image_url.startswith("https://"):
            raise ValueError("Wikimedia images must use HTTPS.")
        response = await self._client.get(result.image_url, timeout=30.0)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if not content_type.startswith("image/"):
            raise ValueError("The selected Wikimedia result is not an image.")
        if len(response.content) > MAX_DOWNLOAD_BYTES:
            raise ValueError("The selected Wikimedia image is too large.")
        return normalize_image(response.content)
