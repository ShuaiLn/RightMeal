import io

import pytest
from PIL import Image

from models.photo_analysis import BoundingRegion
from services.photo_images import crop_region, normalize_image


def source_image() -> bytes:
    image = Image.new("RGB", (120, 80), "red")
    exif = image.getexif()
    exif[0x010E] = "sensitive description"
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


def test_normalization_removes_exif_and_produces_stable_hash():
    first = normalize_image(source_image())
    second = normalize_image(source_image())
    assert first.sha256 == second.sha256
    with Image.open(io.BytesIO(first.content)) as normalized:
        assert not normalized.getexif()
        assert normalized.size == (120, 80)


def test_crop_uses_normalized_region_and_rejects_invalid_bounds():
    normalized = normalize_image(source_image())
    cropped = crop_region(normalized.content, BoundingRegion(0.25, 0.25, 0.75, 0.75))
    with Image.open(io.BytesIO(cropped)) as image:
        assert image.size == (60, 40)
    with pytest.raises(ValueError):
        crop_region(normalized.content, BoundingRegion(-0.1, 0.0, 1.0, 1.0))


def test_invalid_image_is_rejected():
    with pytest.raises(ValueError):
        normalize_image(b"not an image")
