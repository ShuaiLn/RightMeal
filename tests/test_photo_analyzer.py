"""Evidence-only photo analyzer request and strict validation tests."""

import io
import json

import httpx
from PIL import Image

from conftest import openai_client
from models import HouseholdProfile
from models.photo_analysis import PhotoKind
from services.photo_analyzer import PhotoAnalyzer, RESPONSE_SCHEMA, get_photo_analyzer


def image_bytes(width=500, height=800) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="JPEG")
    return output.getvalue()


def weight(value=907.2, unit="g", raw="907.2 g"):
    return {"value": value, "unit": unit, "raw_text": raw}


def product_payload(**overrides) -> dict:
    product = {
        "observed_name": "Great Value Long Grain Rice",
        "generic_food_name": "white rice",
        "brand": "Great Value",
        "language": "en",
        "form": "dry",
        "package_text": "2 lb bag",
        "quantity": 1,
        "total_weight": weight(),
        "unit_weight": None,
        "printed_price": None,
        "printed_currency": None,
        "visible_evidence": ["Long Grain Rice", "NET WT 2 LB"],
    }
    product.update(overrides)
    return {
        "kind": "product",
        "product": product,
        "receipt": None,
        "observed_summary": "A bag of dry white rice.",
    }


def receipt_payload(lines=3, estimate=3, **overrides) -> dict:
    values = []
    for index in range(lines):
        top = 0.15 + index * 0.12
        values.append({
            "source_line_index": index,
            "bounding_region": [0.1, top, 0.9, top + 0.08],
            "raw_printed_text": f"ITEM {index} 2.99",
            "generic_item_name": "food item",
            "brand": None,
            "language": "en",
            "form": "unknown",
            "quantity": 1,
            "total_weight": None,
            "unit_weight": None,
            "printed_line_total": 2.99,
            "classification": "merchandise",
        })
    receipt = {
        "store_name": "Example Market",
        "purchase_date": "2026-07-14",
        "currency": "USD",
        "estimated_visible_merchandise_line_count": estimate,
        "merchandise_area": [0.05, 0.1, 0.95, 0.5],
        "bottom_visible": True,
        "lines": values,
    }
    receipt.update(overrides)
    return {
        "kind": "receipt",
        "product": None,
        "receipt": receipt,
        "observed_summary": "A grocery receipt segment.",
    }


class TestAnalyze:
    async def test_product_happy_path_and_exif_free_normalization(self):
        result = await PhotoAnalyzer(
            "sk-test", openai_client(product_payload())
        ).analyze_product(image_bytes(), "image/jpeg")
        assert result is not None
        assert result.kind is PhotoKind.PRODUCT
        assert result.product.generic_food_name == "white rice"
        assert result.product.brand == "Great Value"
        assert result.image.mime == "image/jpeg"
        assert len(result.image.sha256) == 64

    async def test_receipt_coverage_is_local_and_blocks_incomplete_result(self):
        complete = await PhotoAnalyzer(
            "sk-test", openai_client(receipt_payload())
        ).analyze_receipt(image_bytes(), "image/jpeg")
        assert complete is not None
        assert complete.coverage.complete

        incomplete = await PhotoAnalyzer(
            "sk-test", openai_client(receipt_payload(lines=1, estimate=8))
        ).analyze_receipt(image_bytes(), "image/jpeg")
        assert incomplete is not None
        assert not incomplete.confirmable
        assert any("approximately 8" in reason for reason in incomplete.coverage.reasons)

    async def test_strict_parser_rejects_extra_or_missing_fields(self):
        extra = product_payload()
        extra["product"]["matched_food_id"] = "rice_white"
        assert await PhotoAnalyzer(
            "sk-test", openai_client(extra)
        ).analyze_product(image_bytes(), "image/jpeg") is None

        missing = product_payload()
        del missing["product"]["visible_evidence"]
        assert await PhotoAnalyzer(
            "sk-test", openai_client(missing)
        ).analyze_product(image_bytes(), "image/jpeg") is None

    async def test_invalid_coordinates_and_over_30_lines_are_incomplete(self):
        payload = receipt_payload(lines=1, estimate=31)
        payload["receipt"]["lines"][0]["bounding_region"] = [-0.1, 0.2, 0.9, 0.3]
        result = await PhotoAnalyzer(
            "sk-test", openai_client(payload)
        ).analyze_receipt(image_bytes(), "image/jpeg")
        assert result is not None
        assert not result.coverage.complete
        assert any("more than 30" in reason for reason in result.coverage.reasons)
        assert any("invalid coordinates" in reason for reason in result.coverage.reasons)

    async def test_empty_invalid_and_http_error_return_none(self):
        analyzer = PhotoAnalyzer("sk-test", openai_client(product_payload()))
        assert await analyzer.analyze_product(b"", "image/jpeg") is None
        assert await analyzer.analyze_product(b"not an image", "image/jpeg") is None
        assert await PhotoAnalyzer(
            "sk-test", openai_client(status=500)
        ).analyze_product(image_bytes(), "image/jpeg") is None


class TestRequestShape:
    async def test_request_has_no_catalog_content_and_uses_strict_schema(self, foods):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(product_payload())}}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await PhotoAnalyzer("sk-test", client).analyze_product(
            image_bytes(), "image/jpeg", foods
        )
        assert result is not None
        assert captured["response_format"]["json_schema"]["strict"] is True
        serialized = json.dumps(captured).casefold()
        assert "rice_white" not in serialized
        assert all(food.id.casefold() not in serialized for food in foods)
        user_text = captured["messages"][1]["content"][0]["text"]
        assert "catalog" not in user_text.casefold()
        assert captured["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )

    def test_response_schema_has_no_catalog_properties(self):
        serialized = json.dumps(RESPONSE_SCHEMA).casefold()
        for forbidden in ("matched_food_id", "candidate", "match_score", "embedding"):
            assert forbidden not in serialized


class TestFactory:
    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert get_photo_analyzer(None, openai_client({})) is None

    def test_profile_key_wins(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        profile = HouseholdProfile(adults=1, api_keys={"openai_api_key": "sk-test"})
        assert get_photo_analyzer(profile, openai_client({})) is not None
