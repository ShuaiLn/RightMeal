"""Evidence-only OpenAI vision extraction for product and receipt photos.

The model never receives the local catalog and cannot return a catalog ID,
candidate, score, compatibility decision, or matching explanation. Its output
is strictly validated, then matched against the catalog by local code.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Sequence

import httpx

from models.photo_analysis import (
    CoverageValidation,
    PhotoAnalysis,
    PhotoKind,
    ProductFacts,
    ReceiptFacts,
    photo_analysis_from_dict,
)
from models.profile import HouseholdProfile
from services.keys import resolve_key
from services.photo_images import MAX_NORMALIZED_BYTES, NormalizedImage, normalize_image
from services.receipt_validation import (
    flag_intra_segment_duplicates,
    validate_receipt_coverage,
)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT_SECONDS = 45.0
MAX_IMAGE_BYTES = MAX_NORMALIZED_BYTES

SYSTEM_PROMPT = (
    "Extract only directly observable facts from one grocery product photo or "
    "receipt segment. Do not identify or match any application catalog item. "
    "Do not return IDs, candidates, match scores, form compatibility decisions, "
    "or match explanations. Never infer hidden text, weights, prices, discounts, "
    "or receipt totals. Preserve printed weight units and raw printed text. "
    "For receipts, report every visible line in the merchandise area, classify "
    "each line, estimate the number of visible merchandise lines independently, "
    "and use normalized image coordinates. Do not transcribe names, addresses, "
    "card or member numbers, QR contents, or transaction tokens. Use null for "
    "facts that are not clearly visible. Respond only with the supplied schema."
)

_WEIGHT_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "value": {"type": "number", "exclusiveMinimum": 0},
        "unit": {"type": "string"},
        "raw_text": {"type": "string"},
    },
    "required": ["value", "unit", "raw_text"],
}

_FORM_SCHEMA = {
    "type": "string",
    "enum": ["fresh", "dry", "canned", "frozen", "cooked", "prepared", "unknown"],
}

_PRODUCT_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "observed_name": {"type": "string"},
        "generic_food_name": {"type": "string"},
        "brand": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
        "form": _FORM_SCHEMA,
        "package_text": {"type": ["string", "null"]},
        "quantity": {"type": ["integer", "null"], "minimum": 1},
        "total_weight": _WEIGHT_SCHEMA,
        "unit_weight": _WEIGHT_SCHEMA,
        "printed_price": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "printed_currency": {"type": ["string", "null"]},
        "visible_evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "observed_name", "generic_food_name", "brand", "language", "form",
        "package_text", "quantity", "total_weight", "unit_weight",
        "printed_price", "printed_currency", "visible_evidence",
    ],
}

_RECEIPT_LINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_line_index": {"type": "integer", "minimum": 0},
        "bounding_region": {
            "type": "array",
            "items": {"type": "number", "minimum": 0, "maximum": 1},
            "minItems": 4,
            "maxItems": 4,
        },
        "raw_printed_text": {"type": "string"},
        "generic_item_name": {"type": "string"},
        "brand": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
        "form": _FORM_SCHEMA,
        "quantity": {"type": ["integer", "null"], "minimum": 1},
        "total_weight": _WEIGHT_SCHEMA,
        "unit_weight": _WEIGHT_SCHEMA,
        "printed_line_total": {
            "type": ["number", "null"],
            "exclusiveMinimum": 0,
        },
        "classification": {
            "type": "string",
            "enum": [
                "merchandise", "coupon", "loyalty_discount", "tax", "deposit",
                "subtotal", "total", "return", "other",
            ],
        },
    },
    "required": [
        "source_line_index", "bounding_region", "raw_printed_text",
        "generic_item_name", "brand", "language", "form", "quantity",
        "total_weight", "unit_weight", "printed_line_total", "classification",
    ],
}

_RECEIPT_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "store_name": {"type": ["string", "null"]},
        "purchase_date": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "estimated_visible_merchandise_line_count": {
            "type": "integer",
            "minimum": 0,
        },
        "merchandise_area": {
            "type": "array",
            "items": {"type": "number", "minimum": 0, "maximum": 1},
            "minItems": 4,
            "maxItems": 4,
        },
        "bottom_visible": {"type": "boolean"},
        "lines": {"type": "array", "items": _RECEIPT_LINE_SCHEMA},
    },
    "required": [
        "store_name", "purchase_date", "currency",
        "estimated_visible_merchandise_line_count", "merchandise_area",
        "bottom_visible", "lines",
    ],
}

RESPONSE_SCHEMA = {
    "name": "evidence_only_photo_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["product", "receipt", "non_purchase", "unreadable"],
            },
            "product": _PRODUCT_SCHEMA,
            "receipt": _RECEIPT_SCHEMA,
            "observed_summary": {"type": "string"},
        },
        "required": ["kind", "product", "receipt", "observed_summary"],
    },
}

# The old code exported two schema constants. Keeping the aliases avoids import
# breakage while both entry points now share the one evidence-only interface.
RECEIPT_RESPONSE_SCHEMA = RESPONSE_SCHEMA
RECEIPT_SYSTEM_PROMPT = SYSTEM_PROMPT


@dataclass(frozen=True)
class AnalyzedPhoto:
    analysis: PhotoAnalysis
    image: NormalizedImage
    coverage: CoverageValidation | None = None

    @property
    def kind(self) -> PhotoKind:
        return self.analysis.kind

    @property
    def product(self) -> ProductFacts | None:
        return self.analysis.product

    @property
    def receipt(self) -> ReceiptFacts | None:
        return self.analysis.receipt

    @property
    def confirmable(self) -> bool:
        return self.coverage is None or self.coverage.complete


class PhotoAnalyzer:
    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        model: str = DEFAULT_MODEL,
    ):
        self._api_key = api_key
        self._client = http_client
        self._model = model

    async def analyze(
        self,
        image_bytes: bytes,
        mime: str | None = None,
        *,
        expected_kind: PhotoKind | None = None,
    ) -> AnalyzedPhoto | None:
        """Analyze a photo, returning None on decoding, HTTP, or schema failure."""

        try:
            image = normalize_image(image_bytes)
            data = await self._post_image(image, expected_kind=expected_kind)
            analysis = photo_analysis_from_dict(data)
            coverage = None
            if analysis.receipt is not None:
                receipt = flag_intra_segment_duplicates(analysis.receipt)
                analysis = PhotoAnalysis(
                    kind=analysis.kind,
                    product=analysis.product,
                    receipt=receipt,
                    observed_summary=analysis.observed_summary,
                )
                coverage = validate_receipt_coverage(receipt, image.width, image.height)
            return AnalyzedPhoto(analysis=analysis, image=image, coverage=coverage)
        except Exception:  # Any extraction failure falls back to manual entry.
            return None

    async def analyze_product(
        self,
        image_bytes: bytes,
        mime: str | None = None,
        foods: Sequence[object] | None = None,
    ) -> AnalyzedPhoto | None:
        """Analyze a requested product photo; ``foods`` is ignored by design."""

        del foods
        result = await self.analyze(
            image_bytes, mime, expected_kind=PhotoKind.PRODUCT
        )
        if result is None or result.kind is not PhotoKind.PRODUCT:
            return None
        return result

    async def analyze_receipt(
        self,
        image_bytes: bytes,
        mime: str | None = None,
        foods: Sequence[object] | None = None,
    ) -> AnalyzedPhoto | None:
        """Analyze one receipt image or segment; ``foods`` is never transmitted."""

        del foods
        result = await self.analyze(
            image_bytes, mime, expected_kind=PhotoKind.RECEIPT
        )
        if result is None or result.kind is not PhotoKind.RECEIPT:
            return None
        return result

    async def _post_image(
        self,
        image: NormalizedImage,
        *,
        expected_kind: PhotoKind | None,
    ) -> dict:
        data_url = (
            f"data:{image.mime};base64,"
            f"{base64.b64encode(image.content).decode('ascii')}"
        )
        kind_instruction = (
            f"The user selected this as a {expected_kind.value} photo. "
            "Report the actual kind if that selection is wrong."
            if expected_kind is not None
            else "Report the actual photo kind."
        )
        response = await self._client.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": kind_instruction},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    },
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": RESPONSE_SCHEMA,
                },
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("The model response is not an object.")
        return parsed


def get_photo_analyzer(
    profile: HouseholdProfile | None,
    http_client: httpx.AsyncClient,
) -> PhotoAnalyzer | None:
    """Return an analyzer when an OpenAI key is configured."""

    api_key = resolve_key("openai_api_key", profile)
    if not api_key:
        return None
    return PhotoAnalyzer(api_key, http_client=http_client)
