import io
import json

import httpx
import pytest
from PIL import Image

from services.wikimedia_images import WikimediaImageSearch


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (40, 40), "green").save(output, format="PNG")
    return output.getvalue()


def search_payload(url="https://upload.wikimedia.org/example.png", mime="image/png"):
    return {
        "query": {
            "pages": [{
                "title": "File:Example food.png",
                "imageinfo": [{
                    "url": url,
                    "thumburl": url,
                    "mime": mime,
                    "width": 800,
                    "height": 600,
                    "extmetadata": {
                        "Artist": {"value": "Example Author"},
                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                    },
                }],
            }],
        },
    }


async def test_search_uses_mediawiki_api_and_requires_user_selectable_metadata():
    captured = {}

    def handler(request):
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=search_payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results = await WikimediaImageSearch(client).search("lentils")
    assert len(results) == 1
    assert captured["action"] == "query"
    assert captured["generator"] == "search"
    assert captured["gsrnamespace"] == "6"
    assert "filetype:bitmap" in captured["gsrsearch"]
    assert results[0].author == "Example Author"
    assert results[0].license_name == "CC BY-SA 4.0"
    assert results[0].source_page.startswith("https://commons.wikimedia.org/wiki/")


async def test_search_rejects_non_https_and_non_images():
    values = search_payload(url="http://example.test/file.txt", mime="text/plain")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=values))
    )
    assert await WikimediaImageSearch(client).search("food") == ()


async def test_download_validates_mime_and_decodes_image():
    result_payload = search_payload()

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json=result_payload)
        return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = WikimediaImageSearch(client)
    result = (await service.search("food"))[0]
    image = await service.download(result)
    assert image.mime == "image/jpeg"
    assert image.width == 40

    bad_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"no", headers={"content-type": "text/plain"})
        )
    )
    with pytest.raises(ValueError):
        await WikimediaImageSearch(bad_client).download(result)
