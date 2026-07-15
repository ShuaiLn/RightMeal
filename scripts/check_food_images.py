"""Manual dev check: every catalog food image URL must resolve to a real image.

Run from the repo root:  .venv/Scripts/python.exe scripts/check_food_images.py
Kept out of pytest on purpose — it hits the live TheMealDB CDN.

Does a real GET (not HEAD): some CDNs serve a placeholder/error page as 200
with a spoofed image content-type, so status + content-type alone can't be
trusted. The body's magic bytes are checked against the common raster formats
TheMealDB serves (PNG/JPEG/GIF/WEBP) as a lightweight decode proxy, since this
project has no Pillow dependency to do a full image-library decode.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from data.loader import load_catalog  # noqa: E402

_SIGNATURES = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "gif": b"GIF8",
    "webp": b"RIFF",  # followed by "WEBP" at offset 8; checked separately below
}


def _looks_like_image(content: bytes) -> bool:
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return True
    return any(content.startswith(sig) for fmt, sig in _SIGNATURES.items() if fmt != "webp")


def check(client: httpx.Client, url: str) -> tuple[bool, str]:
    try:
        response = client.get(url, follow_redirects=True, timeout=15.0)
    except httpx.HTTPError as exc:
        return False, f"error: {type(exc).__name__}"
    content_type = response.headers.get("content-type", "")
    ok = (
        response.status_code == 200
        and content_type.startswith("image/")
        and bool(response.content)
        and _looks_like_image(response.content)
    )
    return ok, f"{response.status_code} {content_type} {len(response.content)}b"


def main() -> int:
    failures = 0
    with httpx.Client() as client:
        for food in load_catalog():
            if not food.image_url:
                failures += 1
                print(f"FAIL  {food.id:<22} no image_url")
                continue
            ok, detail = check(client, food.image_url)
            print(f"{'ok  ' if ok else 'FAIL'}  {food.id:<22} {detail}  {food.image_url}")
            if not ok:
                failures += 1
    print(f"\n{'All good' if failures == 0 else f'{failures} failures'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
