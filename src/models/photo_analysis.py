"""Evidence-only facts extracted from a product or receipt photo.

These models deliberately have no catalog identifiers, candidates, scores, or
matching explanations. Catalog matching is a separate local operation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable


class PhotoKind(str, Enum):
    PRODUCT = "product"
    RECEIPT = "receipt"
    NON_PURCHASE = "non_purchase"
    UNREADABLE = "unreadable"


class FoodForm(str, Enum):
    FRESH = "fresh"
    DRY = "dry"
    CANNED = "canned"
    FROZEN = "frozen"
    COOKED = "cooked"
    PREPARED = "prepared"
    UNKNOWN = "unknown"


class ReceiptLineClassification(str, Enum):
    MERCHANDISE = "merchandise"
    COUPON = "coupon"
    LOYALTY_DISCOUNT = "loyalty_discount"
    TAX = "tax"
    DEPOSIT = "deposit"
    SUBTOTAL = "subtotal"
    TOTAL = "total"
    RETURN = "return"
    OTHER = "other"


@dataclass(frozen=True)
class BoundingRegion:
    """Normalized image coordinates in left, top, right, bottom order."""

    x1: float
    y1: float
    x2: float
    y2: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    def is_valid(self) -> bool:
        return (
            0.0 <= self.x1 < self.x2 <= 1.0
            and 0.0 <= self.y1 < self.y2 <= 1.0
        )

    def contains(self, other: "BoundingRegion") -> bool:
        return (
            self.x1 <= other.x1
            and self.y1 <= other.y1
            and self.x2 >= other.x2
            and self.y2 >= other.y2
        )

    def intersection_over_union(self, other: "BoundingRegion") -> float:
        left = max(self.x1, other.x1)
        top = max(self.y1, other.y1)
        right = min(self.x2, other.x2)
        bottom = min(self.y2, other.y2)
        if right <= left or bottom <= top:
            return 0.0
        intersection = (right - left) * (bottom - top)
        union = self.width * self.height + other.width * other.height - intersection
        return intersection / union if union > 0 else 0.0


@dataclass(frozen=True)
class WeightFact:
    """A printed or visibly observed weight, without catalog assumptions."""

    value: float
    unit: str
    raw_text: str


@dataclass(frozen=True)
class ProductFacts:
    observed_name: str
    generic_food_name: str
    brand: str | None
    language: str | None
    form: FoodForm
    package_text: str | None
    quantity: int | None
    total_weight: WeightFact | None
    unit_weight: WeightFact | None
    printed_price: float | None
    printed_currency: str | None
    visible_evidence: tuple[str, ...]


@dataclass(frozen=True)
class ReceiptLineFacts:
    source_line_index: int
    bounding_region: BoundingRegion
    raw_printed_text: str
    generic_item_name: str
    brand: str | None
    language: str | None
    form: FoodForm
    quantity: int | None
    total_weight: WeightFact | None
    unit_weight: WeightFact | None
    printed_line_total: float | None
    classification: ReceiptLineClassification
    # These fields are derived locally and are never model output.
    segment_index: int = 0
    possible_duplicate: bool = False
    duplicate_reason: str | None = None


@dataclass(frozen=True)
class ReceiptFacts:
    store_name: str | None
    purchase_date: str | None
    currency: str | None
    estimated_visible_merchandise_line_count: int
    merchandise_area: BoundingRegion
    bottom_visible: bool
    lines: tuple[ReceiptLineFacts, ...]


@dataclass(frozen=True)
class PhotoAnalysis:
    kind: PhotoKind
    product: ProductFacts | None
    receipt: ReceiptFacts | None
    observed_summary: str


@dataclass(frozen=True)
class CoverageValidation:
    complete: bool
    reasons: tuple[str, ...] = ()


def _strict_keys(data: dict[str, Any], required: Iterable[str], label: str) -> None:
    expected = set(required)
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{label} fields differ; missing={missing}, extra={extra}")


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")
    value = value.strip()
    return value or None


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value.strip()


def _optional_positive_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number or null")
    value = float(value)
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _optional_positive_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer or null")
    return value


def _parse_region(value: Any, label: str) -> BoundingRegion:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label} must contain four coordinates")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise ValueError(f"{label} coordinates must be numbers")
    return BoundingRegion(*(float(v) for v in value))


def _parse_weight(value: Any, label: str) -> WeightFact | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object or null")
    _strict_keys(value, ("value", "unit", "raw_text"), label)
    number = _optional_positive_number(value["value"], f"{label}.value")
    assert number is not None
    unit = _required_string(value["unit"], f"{label}.unit")
    raw_text = _required_string(value["raw_text"], f"{label}.raw_text")
    if not unit or not raw_text:
        raise ValueError(f"{label} unit and raw_text must not be empty")
    return WeightFact(number, unit, raw_text)


def photo_analysis_from_dict(data: Any) -> PhotoAnalysis:
    """Strictly validate and parse the evidence-only model response."""

    if not isinstance(data, dict):
        raise ValueError("photo analysis must be an object")
    _strict_keys(data, ("kind", "product", "receipt", "observed_summary"), "analysis")
    try:
        kind = PhotoKind(data["kind"])
    except (TypeError, ValueError) as exc:
        raise ValueError("unknown photo kind") from exc

    product: ProductFacts | None = None
    raw_product = data["product"]
    if raw_product is not None:
        if not isinstance(raw_product, dict):
            raise ValueError("product must be an object or null")
        fields = (
            "observed_name", "generic_food_name", "brand", "language", "form",
            "package_text", "quantity", "total_weight", "unit_weight",
            "printed_price", "printed_currency", "visible_evidence",
        )
        _strict_keys(raw_product, fields, "product")
        evidence = raw_product["visible_evidence"]
        if not isinstance(evidence, list) or not all(isinstance(v, str) for v in evidence):
            raise ValueError("visible_evidence must be an array of strings")
        try:
            form = FoodForm(raw_product["form"])
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown product form") from exc
        product = ProductFacts(
            observed_name=_required_string(raw_product["observed_name"], "observed_name"),
            generic_food_name=_required_string(
                raw_product["generic_food_name"], "generic_food_name"
            ),
            brand=_optional_string(raw_product["brand"], "brand"),
            language=_optional_string(raw_product["language"], "language"),
            form=form,
            package_text=_optional_string(raw_product["package_text"], "package_text"),
            quantity=_optional_positive_integer(raw_product["quantity"], "quantity"),
            total_weight=_parse_weight(raw_product["total_weight"], "total_weight"),
            unit_weight=_parse_weight(raw_product["unit_weight"], "unit_weight"),
            printed_price=_optional_positive_number(
                raw_product["printed_price"], "printed_price"
            ),
            printed_currency=_optional_string(
                raw_product["printed_currency"], "printed_currency"
            ),
            visible_evidence=tuple(v.strip() for v in evidence if v.strip()),
        )

    receipt: ReceiptFacts | None = None
    raw_receipt = data["receipt"]
    if raw_receipt is not None:
        if not isinstance(raw_receipt, dict):
            raise ValueError("receipt must be an object or null")
        fields = (
            "store_name", "purchase_date", "currency",
            "estimated_visible_merchandise_line_count", "merchandise_area",
            "bottom_visible", "lines",
        )
        _strict_keys(raw_receipt, fields, "receipt")
        estimate = raw_receipt["estimated_visible_merchandise_line_count"]
        if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
            raise ValueError("estimated line count must be a non-negative integer")
        if not isinstance(raw_receipt["bottom_visible"], bool):
            raise ValueError("bottom_visible must be a boolean")
        raw_lines = raw_receipt["lines"]
        if not isinstance(raw_lines, list):
            raise ValueError("receipt lines must be an array")
        lines: list[ReceiptLineFacts] = []
        line_fields = (
            "source_line_index", "bounding_region", "raw_printed_text",
            "generic_item_name", "brand", "language", "form", "quantity",
            "total_weight", "unit_weight", "printed_line_total", "classification",
        )
        for raw_line in raw_lines:
            if not isinstance(raw_line, dict):
                raise ValueError("receipt line must be an object")
            _strict_keys(raw_line, line_fields, "receipt line")
            source_index = raw_line["source_line_index"]
            if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0:
                raise ValueError("source_line_index must be a non-negative integer")
            try:
                form = FoodForm(raw_line["form"])
                classification = ReceiptLineClassification(raw_line["classification"])
            except (TypeError, ValueError) as exc:
                raise ValueError("unknown receipt line form or classification") from exc
            lines.append(ReceiptLineFacts(
                source_line_index=source_index,
                bounding_region=_parse_region(raw_line["bounding_region"], "bounding_region"),
                raw_printed_text=_required_string(
                    raw_line["raw_printed_text"], "raw_printed_text"
                ),
                generic_item_name=_required_string(
                    raw_line["generic_item_name"], "generic_item_name"
                ),
                brand=_optional_string(raw_line["brand"], "brand"),
                language=_optional_string(raw_line["language"], "language"),
                form=form,
                quantity=_optional_positive_integer(raw_line["quantity"], "quantity"),
                total_weight=_parse_weight(raw_line["total_weight"], "total_weight"),
                unit_weight=_parse_weight(raw_line["unit_weight"], "unit_weight"),
                printed_line_total=_optional_positive_number(
                    raw_line["printed_line_total"], "printed_line_total"
                ),
                classification=classification,
            ))
        receipt = ReceiptFacts(
            store_name=_optional_string(raw_receipt["store_name"], "store_name"),
            purchase_date=_optional_string(raw_receipt["purchase_date"], "purchase_date"),
            currency=_optional_string(raw_receipt["currency"], "currency"),
            estimated_visible_merchandise_line_count=estimate,
            merchandise_area=_parse_region(
                raw_receipt["merchandise_area"], "merchandise_area"
            ),
            bottom_visible=raw_receipt["bottom_visible"],
            lines=tuple(lines),
        )

    if kind is PhotoKind.PRODUCT and (product is None or receipt is not None):
        raise ValueError("product analysis must contain product facts only")
    if kind is PhotoKind.RECEIPT and (receipt is None or product is not None):
        raise ValueError("receipt analysis must contain receipt facts only")
    if kind in (PhotoKind.NON_PURCHASE, PhotoKind.UNREADABLE) and (
        product is not None or receipt is not None
    ):
        raise ValueError("non-purchase or unreadable analysis cannot contain purchase facts")
    return PhotoAnalysis(
        kind=kind,
        product=product,
        receipt=receipt,
        observed_summary=_required_string(data["observed_summary"], "observed_summary"),
    )


def with_segment(
    lines: Iterable[ReceiptLineFacts], segment_index: int
) -> tuple[ReceiptLineFacts, ...]:
    return tuple(replace(line, segment_index=segment_index) for line in lines)
