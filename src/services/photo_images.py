"""Decode, sanitize, hash, re-encode, and crop imported photos locally."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from models.photo_analysis import BoundingRegion

MAX_SOURCE_PIXELS = 40_000_000
MAX_NORMALIZED_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class NormalizedImage:
    content: bytes
    mime: str
    extension: str
    width: int
    height: int
    sha256: str


def normalize_image(image_bytes: bytes) -> NormalizedImage:
    """Return a decoded, orientation-corrected image with metadata removed.

    Encoding is deterministic for the same decoded pixels, which makes the
    sanitized hash stable and suitable for duplicate-import checks.
    """

    if not image_bytes:
        raise ValueError("The image is empty.")
    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            opened.load()
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
                raise ValueError("The image dimensions are not supported.")
            image = ImageOps.exif_transpose(opened)
            # Transparency is preserved only for PNG. All metadata, including
            # EXIF, ICC profiles, comments, and textual chunks, is omitted.
            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            output = io.BytesIO()
            if has_alpha:
                image.convert("RGBA").save(output, format="PNG", optimize=True)
                mime, extension = "image/png", ".png"
            else:
                image.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=90,
                    optimize=True,
                    progressive=False,
                    exif=b"",
                )
                mime, extension = "image/jpeg", ".jpg"
            content = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("The selected file is not a readable image.") from exc
    if len(content) > MAX_NORMALIZED_BYTES:
        raise ValueError("The normalized image is larger than 8 MB.")
    return NormalizedImage(
        content=content,
        mime=mime,
        extension=extension,
        width=image.width,
        height=image.height,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def crop_region(image_bytes: bytes, region: BoundingRegion) -> bytes:
    """Return a metadata-free PNG crop for a validated normalized region."""

    if not region.is_valid():
        raise ValueError("The crop region is invalid.")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            left = max(0, min(image.width - 1, round(region.x1 * image.width)))
            top = max(0, min(image.height - 1, round(region.y1 * image.height)))
            right = max(left + 1, min(image.width, round(region.x2 * image.width)))
            bottom = max(top + 1, min(image.height, round(region.y2 * image.height)))
            crop = image.crop((left, top, right, bottom))
            output = io.BytesIO()
            crop.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The image could not be cropped.") from exc
