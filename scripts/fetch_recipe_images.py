"""Download finished-dish images for recipes into the bundled assets.

Dev-time only. Reads recipe_index.json, downloads each recipe's dish image from
the public-domain-recipes origin into src/assets/recipe_images/<slug>.webp, so
Meal cards can show a real finished dish (never an ingredient packaging photo).
Idempotent: existing files are skipped.

    python scripts/fetch_recipe_images.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    print("httpx is required (run via the project venv).")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = PROJECT_ROOT / "src" / "data" / "recipe_index.json"
ASSETS_DIR = PROJECT_ROOT / "src" / "assets" / "recipe_images"
ORIGIN = "https://publicdomainrecipes.com/pix/{slug}.webp"
MAX_BYTES = 3_000_000


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    wanted = [
        Path(asset).stem
        for recipe in index["recipes"]
        if (asset := recipe.get("image_asset"))
    ]

    got = skipped = failed = 0
    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        for slug in wanted:
            dest = ASSETS_DIR / f"{slug}.webp"
            if dest.exists() and dest.stat().st_size > 0:
                skipped += 1
                continue
            try:
                resp = client.get(ORIGIN.format(slug=slug))
                ok = (resp.status_code == 200
                      and resp.headers.get("content-type", "").startswith("image/")
                      and 0 < len(resp.content) <= MAX_BYTES)
                if not ok:
                    failed += 1
                    print(f"  ! {slug}: status {resp.status_code}")
                    continue
                dest.write_bytes(resp.content)
                got += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ! {slug}: {type(exc).__name__}")
    print(f"images: {got} downloaded, {skipped} already present, {failed} failed, "
          f"{len(wanted)} referenced -> {ASSETS_DIR}")


if __name__ == "__main__":
    main()
