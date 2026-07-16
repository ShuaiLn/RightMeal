"""Evidence-only OpenAI vision extraction for product and receipt photos.

The model never receives the local catalog and cannot return a catalog ID,
candidate, score, compatibility decision, or matching explanation. Its output
is strictly validated, then matched against the catalog by local code.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from typing import Sequence

import httpx

from models.photo_analysis import (
    CoordinateProtocolError,
    CoverageValidation,
    PhotoAnalysis,
    PhotoKind,
    ProductFacts,
    ReceiptFacts,
    ReceiptDiagnostics,
    ReceiptScanFacts,
    photo_analysis_from_dict,
    receipt_grouping_from_dict,
    receipt_layout_from_dict,
    receipt_scan_from_dict,
)
from models.profile import HouseholdProfile
from services.keys import resolve_key
from services.photo_images import (
    MAX_NORMALIZED_BYTES,
    NormalizedImage,
    horizontal_ink_density_bands,
    normalize_image,
)
from services.receipt_validation import (
    flag_intra_segment_duplicates,
    select_automatic_end_boundary,
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
    "and explicitly declare one coordinate space and one coordinate order that "
    "apply to both the merchandise area and every line. Do not transcribe names, addresses, "
    "card or member numbers, QR contents, or transaction tokens. Use null for "
    "facts that are not clearly visible. Respond only with the supplied schema."
)

RECEIPT_LAYOUT_SYSTEM_PROMPT = (
    "Analyze receipt layout geometry only. Do not perform logical item grouping "
    "and do not return payment, card, member, address, transaction, QR, or token "
    "text. Mark text/header/likely-item-total regions, candidate SUBTOTAL/TOTAL/"
    "explicit merchandise-end regions, privacy-safe tax/summary/payment/tender/"
    "transaction barriers, and the printed item count as a separate diagnostic. "
    "NUMBER OF ITEMS is never an end-boundary candidate. Declare one coordinate "
    "space and order. Respond only with the supplied strict schema."
)

RECEIPT_GROUPING_SYSTEM_PROMPT = (
    "Independently group logical merchandise items and category headers from the "
    "receipt image. Do not use or assume any output from another pass. Merge a "
    "name, SKU/PLU/barcode, weight detail, and final item price when they belong "
    "to one logical price item; keep separate price regions as separate items. "
    "Return a merchandise area estimate, but do not claim the physical receipt "
    "bottom and do not transcribe payment or transaction text. Declare one "
    "coordinate space and order. Respond only with the supplied strict schema."
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
            "items": {"type": "number"},
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
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
        },
        "coordinate_space": {
            "type": "string",
            "enum": ["normalized", "percent", "pixels"],
        },
        "coordinate_order": {
            "type": "string",
            "enum": ["left_top_right_bottom", "top_left_bottom_right"],
        },
        "bottom_visible": {"type": "boolean"},
        "lines": {"type": "array", "items": _RECEIPT_LINE_SCHEMA},
    },
    "required": [
        "store_name", "purchase_date", "currency",
        "estimated_visible_merchandise_line_count", "merchandise_area",
        "coordinate_space", "coordinate_order", "bottom_visible", "lines",
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

_REGION_ONLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_index": {"type": "integer", "minimum": 0},
        "bounding_region": {
            "type": "array", "items": {"type": "number"},
            "minItems": 4, "maxItems": 4,
        },
    },
    "required": ["source_index", "bounding_region"],
}

_BOUNDARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["subtotal", "total", "explicit_end_marker"],
        },
        "source_index": {"type": "integer", "minimum": 0},
        "bounding_region": {
            "type": "array", "items": {"type": "number"},
            "minItems": 4, "maxItems": 4,
        },
    },
    "required": ["kind", "source_index", "bounding_region"],
}

_BARRIER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["tax_summary", "payment_tender", "transaction"],
        },
        "source_index": {"type": "integer", "minimum": 0},
        "bounding_region": {
            "type": "array", "items": {"type": "number"},
            "minItems": 4, "maxItems": 4,
        },
    },
    "required": ["kind", "source_index", "bounding_region"],
}

_LAYOUT_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "coordinate_space": {
            "type": "string", "enum": ["normalized", "percent", "pixels"]
        },
        "coordinate_order": {
            "type": "string",
            "enum": ["left_top_right_bottom", "top_left_bottom_right"],
        },
        "text_regions": {"type": "array", "items": _REGION_ONLY_SCHEMA},
        "header_regions": {"type": "array", "items": _REGION_ONLY_SCHEMA},
        "likely_item_total_regions": {
            "type": "array", "items": _REGION_ONLY_SCHEMA
        },
        "boundary_candidates": {"type": "array", "items": _BOUNDARY_SCHEMA},
        "barriers": {"type": "array", "items": _BARRIER_SCHEMA},
        "printed_item_count": {"type": ["integer", "null"], "minimum": 0},
        "printed_item_count_region": {
            "type": ["array", "null"],
            "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
        },
    },
    "required": [
        "coordinate_space", "coordinate_order", "text_regions", "header_regions",
        "likely_item_total_regions", "boundary_candidates", "barriers",
        "printed_item_count", "printed_item_count_region",
    ],
}

RECEIPT_LAYOUT_RESPONSE_SCHEMA = {
    "name": "receipt_layout_evidence",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["receipt", "non_purchase", "unreadable"],
            },
            "layout": _LAYOUT_SCHEMA,
            "observed_summary": {"type": "string"},
        },
        "required": ["kind", "layout", "observed_summary"],
    },
}

_LOGICAL_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_line_index": {"type": "integer", "minimum": 0},
        "bounding_region": {
            "type": "array", "items": {"type": "number"},
            "minItems": 4, "maxItems": 4,
        },
        "raw_printed_text": {"type": "string"},
        "generic_item_name": {"type": "string"},
        "brand": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
        "form": _FORM_SCHEMA,
        "quantity": {"type": ["integer", "null"], "minimum": 1},
        "total_weight": _WEIGHT_SCHEMA,
        "unit_weight": _WEIGHT_SCHEMA,
        "printed_line_total": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "sku": {"type": ["string", "null"]},
        "plu": {"type": ["string", "null"]},
        "barcode": {"type": ["string", "null"]},
        "price_region": {
            "type": ["array", "null"], "items": {"type": "number"},
            "minItems": 4, "maxItems": 4,
        },
    },
    "required": [
        "source_line_index", "bounding_region", "raw_printed_text",
        "generic_item_name", "brand", "language", "form", "quantity",
        "total_weight", "unit_weight", "printed_line_total", "sku", "plu",
        "barcode", "price_region",
    ],
}

_HEADER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_line_index": {"type": "integer", "minimum": 0},
        "bounding_region": {
            "type": "array", "items": {"type": "number"},
            "minItems": 4, "maxItems": 4,
        },
        "raw_printed_text": {"type": "string"},
        "language": {"type": ["string", "null"]},
    },
    "required": ["source_line_index", "bounding_region", "raw_printed_text", "language"],
}

_GROUPING_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "store_name": {"type": ["string", "null"]},
        "purchase_date": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "coordinate_space": {
            "type": "string", "enum": ["normalized", "percent", "pixels"]
        },
        "coordinate_order": {
            "type": "string",
            "enum": ["left_top_right_bottom", "top_left_bottom_right"],
        },
        "estimated_visible_merchandise_item_count": {
            "type": "integer", "minimum": 0
        },
        "merchandise_area": {
            "type": "array", "items": {"type": "number"},
            "minItems": 4, "maxItems": 4,
        },
        "logical_items": {"type": "array", "items": _LOGICAL_ITEM_SCHEMA},
        "headers": {"type": "array", "items": _HEADER_SCHEMA},
    },
    "required": [
        "store_name", "purchase_date", "currency", "coordinate_space",
        "coordinate_order", "estimated_visible_merchandise_item_count",
        "merchandise_area", "logical_items", "headers",
    ],
}

RECEIPT_GROUPING_RESPONSE_SCHEMA = {
    "name": "receipt_logical_grouping",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["receipt", "non_purchase", "unreadable"],
            },
            "receipt": _GROUPING_SCHEMA,
            "observed_summary": {"type": "string"},
        },
        "required": ["kind", "receipt", "observed_summary"],
    },
}

_RECEIPT_SCAN_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_item_index": {"type": "integer", "minimum": 0, "maximum": 29},
        "raw_printed_text": {"type": "string"},
        "generic_item_name": {"type": "string"},
        "brand": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
        "form": _FORM_SCHEMA,
        "quantity": {"type": ["integer", "null"], "minimum": 1},
        "total_weight": _WEIGHT_SCHEMA,
        "unit_weight": _WEIGHT_SCHEMA,
        "printed_line_total": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "kind": {
            "type": "string",
            "enum": ["food", "non_food", "discount", "summary", "unknown"],
        },
    },
    "required": [
        "source_item_index", "raw_printed_text", "generic_item_name", "brand",
        "language", "form", "quantity", "total_weight", "unit_weight",
        "printed_line_total", "kind",
    ],
}

_COORDINATE_FREE_RECEIPT_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "store_name": {"type": ["string", "null"]},
        "purchase_date": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "unreadable_item_count": {"type": "integer", "minimum": 0},
        "items": {"type": "array", "items": _RECEIPT_SCAN_ITEM_SCHEMA},
    },
    "required": [
        "store_name", "purchase_date", "currency", "unreadable_item_count", "items",
    ],
}

COORDINATE_FREE_RECEIPT_RESPONSE_SCHEMA = {
    "name": "coordinate_free_receipt_scan",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["receipt", "non_purchase", "unreadable"],
            },
            "receipt": _COORDINATE_FREE_RECEIPT_SCHEMA,
            "observed_summary": {"type": "string"},
        },
        "required": ["kind", "receipt", "observed_summary"],
    },
}

COORDINATE_FREE_RECEIPT_SYSTEM_PROMPT = (
    "Extract observable purchased lines from one grocery receipt image or ordered "
    "receipt segment. Return one logical item per printed purchase, in top-to-bottom "
    "order, with stable zero-based source_item_index values. Classify each line as "
    "food, non_food, discount, summary, or unknown. Keep repeated real purchases as "
    "separate items. Never return image coordinates. Never infer hidden names, weights, "
    "quantities, prices, discounts, or totals; use null and increment "
    "unreadable_item_count for merchandise lines that cannot be read. Do not transcribe "
    "names, addresses, member/card numbers, payment details, QR contents, or transaction "
    "tokens. Do not identify or match any application catalog item. Respond only with "
    "the supplied strict schema."
)

# The old code exported two schema constants. Keeping the aliases avoids import
# breakage while both entry points now share the one evidence-only interface.
RECEIPT_RESPONSE_SCHEMA = RECEIPT_LAYOUT_RESPONSE_SCHEMA
RECEIPT_SYSTEM_PROMPT = RECEIPT_LAYOUT_SYSTEM_PROMPT


@dataclass(frozen=True)
class AnalyzedPhoto:
    analysis: PhotoAnalysis
    image: NormalizedImage
    coverage: CoverageValidation | None = None
    diagnostics: ReceiptDiagnostics | None = None
    failure_message: str | None = None

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


@dataclass(frozen=True)
class AnalyzedReceiptSegment:
    receipt: ReceiptScanFacts
    image: NormalizedImage
    observed_summary: str = ""


class ReceiptScanError(RuntimeError):
    """Specific, privacy-safe failure details for the receipt popup."""

    def __init__(
        self,
        code: str,
        stage: str,
        reason: str,
        *,
        suggestions: Sequence[str] = (),
        diagnostics: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.stage = stage
        self.reason = reason
        self.suggestions = tuple(suggestions)
        self.diagnostics = tuple(diagnostics)


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
        except Exception:
            return None
        attempts = 2 if expected_kind is PhotoKind.RECEIPT else 1
        previous_coordinate_error: CoordinateProtocolError | None = None
        for attempt in range(attempts):
            try:
                data = await self._post_image(image, expected_kind=expected_kind)
                analysis = photo_analysis_from_dict(
                    data,
                    image_width=image.width,
                    image_height=image.height,
                )
                coverage = None
                diagnostics = None
                if analysis.receipt is not None:
                    receipt = flag_intra_segment_duplicates(analysis.receipt)
                    analysis = PhotoAnalysis(
                        kind=analysis.kind,
                        product=analysis.product,
                        receipt=receipt,
                        observed_summary=analysis.observed_summary,
                    )
                    coverage = validate_receipt_coverage(
                        receipt, image.width, image.height
                    )
                    diagnostics = ReceiptDiagnostics(
                        coordinate_space=receipt.coordinate_space.value,
                        coordinate_order=receipt.coordinate_order.value,
                        image_width=image.width,
                        image_height=image.height,
                        failure_stage=coverage.failure_stage,
                        raw_merchandise_area=receipt.raw_merchandise_area,
                        retried=attempt > 0,
                    )
                return AnalyzedPhoto(
                    analysis=analysis,
                    image=image,
                    coverage=coverage,
                    diagnostics=diagnostics,
                )
            except CoordinateProtocolError as exc:
                previous_coordinate_error = exc
                if attempt + 1 < attempts:
                    continue
                diagnostics = ReceiptDiagnostics(
                    coordinate_space=exc.coordinate_space,
                    coordinate_order=exc.coordinate_order,
                    image_width=image.width,
                    image_height=image.height,
                    failure_stage="coordinates",
                    raw_merchandise_area=exc.raw_merchandise_area,
                    failure_line_index=exc.line_index,
                    coordinate_reason=exc.reason,
                    retried=attempt > 0,
                )
                return AnalyzedPhoto(
                    analysis=PhotoAnalysis(
                        kind=PhotoKind.UNREADABLE,
                        product=None,
                        receipt=None,
                        observed_summary="",
                    ),
                    image=image,
                    coverage=CoverageValidation(
                        False,
                        (
                            "Receipt coordinates could not be interpreted. "
                            "Please retry the analysis.",
                        ),
                        "coordinates",
                    ),
                    diagnostics=diagnostics,
                    failure_message=(
                        "Receipt coordinates could not be interpreted. "
                        "Please retry the analysis."
                    ),
                )
            except Exception:  # HTTP and non-coordinate schema failures are unreadable.
                return None
        assert previous_coordinate_error is not None
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

    async def scan_receipt(
        self,
        image_bytes: bytes,
        mime: str | None = None,
    ) -> AnalyzedReceiptSegment:
        """Run the live coordinate-free receipt extraction path.

        Unlike the compatibility ``analyze_receipt`` method below, this method
        never requests or validates OCR geometry.  Every failure retains a
        concrete stage and reason so the UI can render a useful modal.
        """

        del mime
        try:
            image = normalize_image(image_bytes)
        except Exception as exc:
            raise ReceiptScanError(
                "image_decode",
                "Image preparation",
                f"The selected file could not be decoded as a supported receipt image: {exc}",
                suggestions=(
                    "Choose a clear JPG or PNG image.",
                    "Open and re-save the image, then try again.",
                ),
            ) from exc

        try:
            data = await self._post_image(
                image,
                expected_kind=PhotoKind.RECEIPT,
                response_schema=COORDINATE_FREE_RECEIPT_RESPONSE_SCHEMA,
                system_prompt=COORDINATE_FREE_RECEIPT_SYSTEM_PROMPT,
                user_instruction=(
                    "Extract the ordered purchased lines only. Do not return coordinates."
                ),
            )
        except httpx.TimeoutException as exc:
            raise ReceiptScanError(
                "api_timeout",
                "OpenAI request",
                "The receipt analysis timed out before OpenAI returned a result.",
                suggestions=("Check the network connection and retry.",),
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                reason = "The OpenAI API key was rejected or is not allowed to use this model."
                suggestions = ("Check the OpenAI key in Profile and try again.",)
            elif status == 429:
                reason = "OpenAI rejected the request because the account is rate-limited or out of quota."
                suggestions = ("Wait and retry, or check the API account quota.",)
            elif status >= 500:
                reason = f"OpenAI returned a temporary server error (HTTP {status})."
                suggestions = ("Retry the receipt scan in a moment.",)
            else:
                reason = f"OpenAI rejected the receipt analysis request (HTTP {status})."
                suggestions = ("Check the API key and retry with a smaller clear image.",)
            raise ReceiptScanError(
                "api_http_error",
                "OpenAI request",
                reason,
                suggestions=suggestions,
                diagnostics=(("HTTP status", str(status)),),
            ) from exc
        except httpx.HTTPError as exc:
            raise ReceiptScanError(
                "api_network_error",
                "OpenAI request",
                f"The receipt analysis request could not reach OpenAI: {exc}",
                suggestions=("Check the network connection and retry.",),
            ) from exc
        except Exception as exc:
            raise ReceiptScanError(
                "api_response_error",
                "OpenAI response",
                f"The receipt analysis response could not be read: {exc}",
                suggestions=("Retry with a clear, upright receipt image.",),
            ) from exc

        actual_kind = str(data.get("kind", ""))
        if actual_kind == PhotoKind.NON_PURCHASE.value:
            raise ReceiptScanError(
                "not_receipt",
                "Content recognition",
                "The selected image was not recognized as a grocery receipt.",
                suggestions=("Choose a photo that clearly shows the printed receipt.",),
            )
        if actual_kind == PhotoKind.UNREADABLE.value:
            raise ReceiptScanError(
                "unreadable_receipt",
                "Content recognition",
                "The receipt text was too unclear to extract safely.",
                suggestions=(
                    "Retake the image in bright, even light.",
                    "Keep the receipt flat, sharp, and upright.",
                ),
            )
        if actual_kind != PhotoKind.RECEIPT.value or data.get("receipt") is None:
            raise ReceiptScanError(
                "invalid_classification",
                "Response validation",
                "OpenAI did not return receipt facts for the selected image.",
                suggestions=("Choose the receipt again and retry.",),
            )
        try:
            receipt = receipt_scan_from_dict(data["receipt"])
        except Exception as exc:
            raise ReceiptScanError(
                "invalid_receipt_schema",
                "Response validation",
                f"The extracted receipt fields were invalid: {exc}",
                suggestions=("Retry the analysis with a clearer receipt image.",),
            ) from exc
        if receipt.unreadable_item_count:
            raise ReceiptScanError(
                "unreadable_items",
                "Receipt item extraction",
                f"{receipt.unreadable_item_count} purchased line(s) could not be read safely.",
                suggestions=(
                    "Retake the image so every purchased line is sharp and visible.",
                    "Use ordered overlapping photos for a long receipt.",
                ),
                diagnostics=(("Unreadable purchased lines", str(receipt.unreadable_item_count)),),
            )
        if not receipt.items:
            raise ReceiptScanError(
                "no_items",
                "Receipt item extraction",
                "No purchased items were found on the receipt image.",
                suggestions=("Use a photo that includes the itemized purchase lines.",),
            )
        return AnalyzedReceiptSegment(
            receipt=receipt,
            image=image,
            observed_summary=str(data.get("observed_summary", "")),
        )

    async def analyze_receipt(
        self,
        image_bytes: bytes,
        mime: str | None = None,
        foods: Sequence[object] | None = None,
    ) -> AnalyzedPhoto | None:
        """Analyze one receipt image or segment; ``foods`` is never transmitted."""

        del foods, mime
        try:
            image = normalize_image(image_bytes)
        except Exception:
            return None
        try:
            first = await self._post_image(
                image,
                expected_kind=PhotoKind.RECEIPT,
                response_schema=RECEIPT_LAYOUT_RESPONSE_SCHEMA,
                system_prompt=RECEIPT_LAYOUT_SYSTEM_PROMPT,
                user_instruction=(
                    "Run the layout-evidence pass only. NUMBER OF ITEMS is a "
                    "diagnostic, never a merchandise boundary."
                ),
            )
        except Exception:
            return None
        # Compatibility with the former one-pass wire protocol. This is useful
        # for existing persisted fixtures and staged clients, but new schema-
        # conforming responses always take the independent two-pass path.
        if set(first) == {"kind", "product", "receipt", "observed_summary"}:
            return await self._legacy_receipt_result(image, first)
        return await self._canonical_receipt_result(image, first)

    async def _legacy_receipt_result(
        self, image: NormalizedImage, first: dict
    ) -> AnalyzedPhoto | None:
        data = first
        for attempt in range(2):
            try:
                analysis = photo_analysis_from_dict(
                    data, image_width=image.width, image_height=image.height
                )
                if analysis.kind is not PhotoKind.RECEIPT or analysis.receipt is None:
                    return None
                receipt = flag_intra_segment_duplicates(analysis.receipt)
                coverage = validate_receipt_coverage(receipt, image.width, image.height)
                return AnalyzedPhoto(
                    analysis=replace(analysis, receipt=receipt),
                    image=image,
                    coverage=coverage,
                    diagnostics=ReceiptDiagnostics(
                        coordinate_space=receipt.coordinate_space.value,
                        coordinate_order=receipt.coordinate_order.value,
                        image_width=image.width,
                        image_height=image.height,
                        failure_stage=coverage.failure_stage,
                        raw_merchandise_area=receipt.raw_merchandise_area,
                        retried=attempt > 0,
                        layout_attempts=attempt + 1,
                        grouping_attempts=attempt + 1,
                    ),
                )
            except CoordinateProtocolError as exc:
                if attempt == 0:
                    try:
                        data = await self._post_image(
                            image,
                            expected_kind=PhotoKind.RECEIPT,
                            response_schema=RESPONSE_SCHEMA,
                            system_prompt=SYSTEM_PROMPT,
                            user_instruction=(
                                "Retry the receipt because the declared coordinate "
                                "protocol was inconsistent. Use the exact same image."
                            ),
                        )
                    except Exception:
                        return None
                    continue
                return self._coordinate_failure(
                    image,
                    exc,
                    layout_attempts=2,
                    grouping_attempts=2,
                )
            except Exception:
                return None
        return None

    async def _canonical_receipt_result(
        self, image: NormalizedImage, first_layout: dict
    ) -> AnalyzedPhoto | None:
        layout_data = first_layout
        layout_attempts = 1
        grouping_attempts = 0
        try:
            if layout_data.get("kind") != PhotoKind.RECEIPT.value:
                return None
            if layout_data.get("layout") is None:
                return None
            layout = receipt_layout_from_dict(
                layout_data["layout"],
                image_width=image.width,
                image_height=image.height,
            )
        except CoordinateProtocolError:
            layout_attempts += 1
            try:
                layout_data = await self._retry_receipt_pass(
                    image,
                    schema=RECEIPT_LAYOUT_RESPONSE_SCHEMA,
                    system_prompt=RECEIPT_LAYOUT_SYSTEM_PROMPT,
                    pass_name="layout",
                    conflict_codes=("invalid_coordinates",),
                    area=None,
                )
                if layout_data.get("kind") != PhotoKind.RECEIPT.value:
                    return None
                layout = receipt_layout_from_dict(
                    layout_data["layout"],
                    image_width=image.width,
                    image_height=image.height,
                )
            except CoordinateProtocolError as final:
                return self._coordinate_failure(
                    image, final, layout_attempts=layout_attempts, grouping_attempts=0
                )
            except Exception:
                return None
        except Exception:
            return None

        try:
            grouping_data = await self._post_image(
                image,
                expected_kind=PhotoKind.RECEIPT,
                response_schema=RECEIPT_GROUPING_RESPONSE_SCHEMA,
                system_prompt=RECEIPT_GROUPING_SYSTEM_PROMPT,
                user_instruction=(
                    "Run the independent logical-item grouping pass only. Do not "
                    "assume or request the layout pass result."
                ),
            )
            grouping_attempts = 1
            if grouping_data.get("kind") != PhotoKind.RECEIPT.value:
                return None
            if grouping_data.get("receipt") is None:
                return None
            receipt = receipt_grouping_from_dict(
                grouping_data["receipt"],
                image_width=image.width,
                image_height=image.height,
                layout=layout,
            )
        except CoordinateProtocolError:
            grouping_attempts += 1
            try:
                grouping_data = await self._retry_receipt_pass(
                    image,
                    schema=RECEIPT_GROUPING_RESPONSE_SCHEMA,
                    system_prompt=RECEIPT_GROUPING_SYSTEM_PROMPT,
                    pass_name="grouping",
                    conflict_codes=("invalid_coordinates",),
                    area=None,
                )
                receipt = receipt_grouping_from_dict(
                    grouping_data["receipt"],
                    image_width=image.width,
                    image_height=image.height,
                    layout=layout,
                )
            except CoordinateProtocolError as final:
                return self._coordinate_failure(
                    image,
                    final,
                    layout_attempts=layout_attempts,
                    grouping_attempts=grouping_attempts,
                )
            except Exception:
                return None
        except Exception:
            return None

        ink_bands = horizontal_ink_density_bands(image.content)

        def finalize(candidate: ReceiptFacts) -> tuple[ReceiptFacts, CoverageValidation]:
            selection = select_automatic_end_boundary(candidate, image.height)
            if selection.boundary is not None:
                candidate = replace(
                    candidate, merchandise_end_boundary=selection.boundary
                )
            candidate = flag_intra_segment_duplicates(candidate)
            return candidate, validate_receipt_coverage(
                candidate,
                image.width,
                image.height,
                ink_density_bands=ink_bands,
            )

        receipt, coverage = finalize(receipt)
        # Missing boundary means "upload the next segment", not a malformed
        # pass. Every other first-pass geometry/count conflict retries only the
        # pass(es) named by local validation, with no raw receipt text supplied.
        should_retry = (
            not coverage.complete
            and bool(coverage.retry_passes)
            and "missing_end_boundary" not in coverage.conflict_codes
        )
        if should_retry:
            try:
                if "layout" in coverage.retry_passes and layout_attempts < 2:
                    layout_data = await self._retry_receipt_pass(
                        image,
                        schema=RECEIPT_LAYOUT_RESPONSE_SCHEMA,
                        system_prompt=RECEIPT_LAYOUT_SYSTEM_PROMPT,
                        pass_name="layout",
                        conflict_codes=coverage.conflict_codes,
                        area=receipt.merchandise_area.as_tuple(),
                    )
                    layout_attempts += 1
                    layout = receipt_layout_from_dict(
                        layout_data["layout"],
                        image_width=image.width,
                        image_height=image.height,
                    )
                if "grouping" in coverage.retry_passes and grouping_attempts < 2:
                    grouping_data = await self._retry_receipt_pass(
                        image,
                        schema=RECEIPT_GROUPING_RESPONSE_SCHEMA,
                        system_prompt=RECEIPT_GROUPING_SYSTEM_PROMPT,
                        pass_name="grouping",
                        conflict_codes=coverage.conflict_codes,
                        area=receipt.merchandise_area.as_tuple(),
                    )
                    grouping_attempts += 1
                receipt = receipt_grouping_from_dict(
                    grouping_data["receipt"],
                    image_width=image.width,
                    image_height=image.height,
                    layout=layout,
                )
                receipt, coverage = finalize(receipt)
            except CoordinateProtocolError as exc:
                return self._coordinate_failure(
                    image,
                    exc,
                    layout_attempts=layout_attempts,
                    grouping_attempts=grouping_attempts,
                )
            except Exception:
                return None

        generic_failure = (
            not coverage.complete
            and coverage.strong_conflict
            and "missing_end_boundary" not in coverage.conflict_codes
        )
        failure_message = None
        if generic_failure:
            failure_message = (
                "Receipt coordinates could not be interpreted. "
                "Please retry the analysis."
            )
            coverage = CoverageValidation(
                False,
                (failure_message,),
                coverage.failure_stage,
                strong_conflict=True,
                conflict_codes=coverage.conflict_codes,
            )
        analysis = PhotoAnalysis(
            kind=PhotoKind.RECEIPT,
            product=None,
            receipt=receipt,
            observed_summary=str(grouping_data.get("observed_summary", "")),
        )
        diagnostics = ReceiptDiagnostics(
            coordinate_space=receipt.coordinate_space.value,
            coordinate_order=receipt.coordinate_order.value,
            image_width=image.width,
            image_height=image.height,
            failure_stage=coverage.failure_stage,
            raw_merchandise_area=receipt.raw_merchandise_area,
            retried=layout_attempts > 1 or grouping_attempts > 1,
            layout_attempts=layout_attempts,
            grouping_attempts=grouping_attempts,
            ink_density_bands=tuple(ink_bands),
        )
        return AnalyzedPhoto(
            analysis=analysis,
            image=image,
            coverage=coverage,
            diagnostics=diagnostics,
            failure_message=failure_message,
        )

    async def _retry_receipt_pass(
        self,
        image: NormalizedImage,
        *,
        schema: dict,
        system_prompt: str,
        pass_name: str,
        conflict_codes: Sequence[str],
        area: tuple[float, ...] | None,
    ) -> dict:
        # Only anomaly types and normalized coordinates are returned to the
        # retry. No output text or content from the other pass is included.
        codes = ", ".join(conflict_codes) or "coordinate_protocol"
        area_note = f" Problem area (normalized): {area}." if area else ""
        return await self._post_image(
            image,
            expected_kind=PhotoKind.RECEIPT,
            response_schema=schema,
            system_prompt=system_prompt,
            user_instruction=(
                f"Retry only the {pass_name} pass because local validation "
                f"reported: {codes}.{area_note} Use the exact same image."
            ),
        )

    @staticmethod
    def _coordinate_failure(
        image: NormalizedImage,
        exc: CoordinateProtocolError,
        *,
        layout_attempts: int,
        grouping_attempts: int,
    ) -> AnalyzedPhoto:
        message = (
            "Receipt coordinates could not be interpreted. "
            "Please retry the analysis."
        )
        return AnalyzedPhoto(
            analysis=PhotoAnalysis(
                kind=PhotoKind.UNREADABLE,
                product=None,
                receipt=None,
                observed_summary="",
            ),
            image=image,
            coverage=CoverageValidation(
                False,
                (message,),
                "coordinates",
                strong_conflict=True,
                conflict_codes=("invalid_coordinates",),
            ),
            diagnostics=ReceiptDiagnostics(
                coordinate_space=exc.coordinate_space,
                coordinate_order=exc.coordinate_order,
                image_width=image.width,
                image_height=image.height,
                failure_stage="coordinates",
                raw_merchandise_area=exc.raw_merchandise_area,
                failure_line_index=exc.line_index,
                coordinate_reason=exc.reason,
                retried=layout_attempts > 1 or grouping_attempts > 1,
                layout_attempts=layout_attempts,
                grouping_attempts=grouping_attempts,
            ),
            failure_message=message,
        )

    async def _post_image(
        self,
        image: NormalizedImage,
        *,
        expected_kind: PhotoKind | None,
        response_schema: dict = RESPONSE_SCHEMA,
        system_prompt: str = SYSTEM_PROMPT,
        user_instruction: str | None = None,
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
        if user_instruction:
            kind_instruction = f"{kind_instruction} {user_instruction}"
        response = await self._client.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
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
                    "json_schema": response_schema,
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
