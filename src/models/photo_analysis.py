"""Evidence-only facts extracted from a product or receipt photo.

These models deliberately have no catalog identifiers, candidates, scores, or
matching explanations. Catalog matching is a separate local operation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
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
    HEADER = "header"
    COUPON = "coupon"
    LOYALTY_DISCOUNT = "loyalty_discount"
    TAX = "tax"
    DEPOSIT = "deposit"
    SUBTOTAL = "subtotal"
    TOTAL = "total"
    RETURN = "return"
    OTHER = "other"


class ReceiptScanItemKind(str, Enum):
    """Coordinate-free classification used by the Pantry receipt scanner."""

    FOOD = "food"
    NON_FOOD = "non_food"
    DISCOUNT = "discount"
    SUMMARY = "summary"
    UNKNOWN = "unknown"


class ReceiptBoundaryKind(str, Enum):
    """The only evidence that may terminate the merchandise region.

    ``USER_CONFIRMED`` is intentionally distinct from the three automatic
    kinds: a manual line must never be persisted or reported as though the
    model saw a subtotal/total marker.
    """

    SUBTOTAL = "subtotal"
    TOTAL = "total"
    EXPLICIT_END_MARKER = "explicit_end_marker"
    USER_CONFIRMED = "user_confirmed_boundary"


class ReceiptBarrierKind(str, Enum):
    TAX_SUMMARY = "tax_summary"
    PAYMENT_TENDER = "payment_tender"
    TRANSACTION = "transaction"


class ReceiptEvidenceKind(str, Enum):
    TEXT = "text"
    HEADER = "header"
    LIKELY_ITEM_TOTAL = "likely_item_total"


class CoordinateSpace(str, Enum):
    NORMALIZED = "normalized"
    PERCENT = "percent"
    PIXELS = "pixels"


class CoordinateOrder(str, Enum):
    LEFT_TOP_RIGHT_BOTTOM = "left_top_right_bottom"
    TOP_LEFT_BOTTOM_RIGHT = "top_left_bottom_right"


class CoordinateProtocolError(ValueError):
    """A declared receipt coordinate protocol could not be interpreted."""

    def __init__(
        self,
        reason: str,
        *,
        coordinate_space: str | None = None,
        coordinate_order: str | None = None,
        raw_merchandise_area: tuple[float, ...] | None = None,
        line_index: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.coordinate_space = coordinate_space
        self.coordinate_order = coordinate_order
        self.raw_merchandise_area = raw_merchandise_area
        self.line_index = line_index


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
            all(math.isfinite(value) for value in self.as_tuple())
            and
            0.0 <= self.x1 < self.x2 <= 1.0
            and 0.0 <= self.y1 < self.y2 <= 1.0
        )

    def contains(self, other: "BoundingRegion", tolerance: float = 0.0) -> bool:
        return (
            self.x1 - tolerance <= other.x1
            and self.y1 - tolerance <= other.y1
            and self.x2 + tolerance >= other.x2
            and self.y2 + tolerance >= other.y2
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
    # Logical-item identifiers. They belong to this price item rather than to
    # separate visible lines, which lets a name/SKU/weight/final-price block be
    # represented as one canonical item.
    sku: str | None = None
    plu: str | None = None
    barcode: str | None = None
    price_region: BoundingRegion | None = None


@dataclass(frozen=True)
class ReceiptScanItem:
    """One ordered receipt item without model-supplied image coordinates.

    The receipt workflow needs observable product facts, not OCR geometry.  A
    segment/index pair is assigned locally and is stable enough for review,
    purchase-event IDs, and overlapping-segment duplicate handling.
    """

    source_item_index: int
    raw_printed_text: str
    generic_item_name: str
    brand: str | None
    language: str | None
    form: FoodForm
    quantity: int | None
    total_weight: WeightFact | None
    unit_weight: WeightFact | None
    printed_line_total: float | None
    kind: ReceiptScanItemKind
    segment_index: int = 0
    possible_duplicate: bool = False
    duplicate_reason: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.source_item_index < 30:
            raise ValueError("receipt item source index must be between 0 and 29")
        if not isinstance(self.kind, ReceiptScanItemKind):
            object.__setattr__(self, "kind", ReceiptScanItemKind(self.kind))


@dataclass(frozen=True)
class ReceiptScanFacts:
    """Observable receipt facts returned by the coordinate-free scan pass."""

    store_name: str | None
    purchase_date: str | None
    currency: str | None
    items: tuple[ReceiptScanItem, ...]
    unreadable_item_count: int = 0

    def __post_init__(self) -> None:
        if self.unreadable_item_count < 0:
            raise ValueError("unreadable receipt item count must be non-negative")
        indexes = [(item.segment_index, item.source_item_index) for item in self.items]
        if len(indexes) != len(set(indexes)):
            raise ValueError("receipt item source indices must be unique")


@dataclass(frozen=True)
class ReceiptEndBoundary:
    kind: ReceiptBoundaryKind
    bounding_region: BoundingRegion
    source_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReceiptBoundaryKind):
            object.__setattr__(self, "kind", ReceiptBoundaryKind(self.kind))
        if self.source_index < 0:
            raise ValueError("receipt boundary source_index must be non-negative")


@dataclass(frozen=True)
class ReceiptBarrier:
    kind: ReceiptBarrierKind
    bounding_region: BoundingRegion
    source_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReceiptBarrierKind):
            object.__setattr__(self, "kind", ReceiptBarrierKind(self.kind))
        if self.source_index < 0:
            raise ValueError("receipt barrier source_index must be non-negative")


@dataclass(frozen=True)
class ReceiptEvidenceRegion:
    kind: ReceiptEvidenceKind
    bounding_region: BoundingRegion
    source_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReceiptEvidenceKind):
            object.__setattr__(self, "kind", ReceiptEvidenceKind(self.kind))
        if self.source_index < 0:
            raise ValueError("receipt evidence source_index must be non-negative")


@dataclass(frozen=True)
class ReceiptLayoutEvidence:
    """Privacy-safe geometry from the independent layout pass.

    It deliberately contains no payment/transaction text. The printed count
    has its own optional region solely for diagnostics; boundary selection is
    forbidden from reading that region.
    """

    text_regions: tuple[ReceiptEvidenceRegion, ...] = ()
    header_regions: tuple[ReceiptEvidenceRegion, ...] = ()
    likely_item_total_regions: tuple[ReceiptEvidenceRegion, ...] = ()
    boundary_candidates: tuple[ReceiptEndBoundary, ...] = ()
    barriers: tuple[ReceiptBarrier, ...] = ()
    printed_item_count: int | None = None
    printed_item_count_region: BoundingRegion | None = None

    def __post_init__(self) -> None:
        if self.printed_item_count is not None and self.printed_item_count < 0:
            raise ValueError("printed_item_count must be non-negative")


@dataclass(frozen=True)
class ReceiptFacts:
    store_name: str | None
    purchase_date: str | None
    currency: str | None
    estimated_visible_merchandise_line_count: int
    merchandise_area: BoundingRegion
    # Deprecated wire compatibility only. New analyses derive end visibility
    # from ``merchandise_end_boundary`` and never trust this model assertion.
    bottom_visible: bool = False
    lines: tuple[ReceiptLineFacts, ...] = ()
    coordinate_space: CoordinateSpace = CoordinateSpace.NORMALIZED
    coordinate_order: CoordinateOrder = CoordinateOrder.LEFT_TOP_RIGHT_BOTTOM
    raw_merchandise_area: tuple[float, ...] | None = None
    logical_items: tuple[ReceiptLineFacts, ...] = ()
    headers: tuple[ReceiptLineFacts, ...] = ()
    merchandise_end_boundary: ReceiptEndBoundary | None = None
    printed_item_count: int | None = None
    estimated_visible_merchandise_item_count: int | None = None
    layout_evidence: ReceiptLayoutEvidence | None = None
    user_confirmed_boundary: ReceiptEndBoundary | None = None

    def __post_init__(self) -> None:
        logical = self.logical_items
        headers = self.headers
        if not logical:
            logical = tuple(
                line for line in self.lines
                if line.classification is ReceiptLineClassification.MERCHANDISE
            )
            object.__setattr__(self, "logical_items", logical)
        if not headers:
            headers = tuple(
                line for line in self.lines
                if line.classification is ReceiptLineClassification.HEADER
            )
            object.__setattr__(self, "headers", headers)
        if not self.lines and (logical or headers):
            object.__setattr__(
                self,
                "lines",
                tuple(sorted((*logical, *headers), key=lambda line: line.source_line_index)),
            )
        if self.printed_item_count is None and self.layout_evidence is not None:
            object.__setattr__(
                self, "printed_item_count", self.layout_evidence.printed_item_count
            )
        if self.estimated_visible_merchandise_item_count is None:
            object.__setattr__(
                self,
                "estimated_visible_merchandise_item_count",
                self.estimated_visible_merchandise_line_count,
            )

    @property
    def merchandise_end_visible(self) -> bool:
        """True only for a reliable automatic or explicitly confirmed line."""

        return (
            self.merchandise_end_boundary is not None
            or self.user_confirmed_boundary is not None
        )


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
    failure_stage: str | None = None
    manual_review_required: bool = False
    strong_conflict: bool = False
    retry_passes: tuple[str, ...] = ()
    conflict_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceiptDiagnostics:
    coordinate_space: str | None
    coordinate_order: str | None
    image_width: int
    image_height: int
    failure_stage: str | None = None
    raw_merchandise_area: tuple[float, ...] | None = None
    failure_line_index: int | None = None
    coordinate_reason: str | None = None
    retried: bool = False
    layout_attempts: int = 1
    grouping_attempts: int = 1
    ink_density_bands: tuple[tuple[float, float], ...] = ()


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


def _raw_region(value: Any, label: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise CoordinateProtocolError(f"{label} must contain four coordinates")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise CoordinateProtocolError(f"{label} coordinates must be numbers")
    raw = tuple(float(v) for v in value)
    if not all(math.isfinite(v) for v in raw):
        raise CoordinateProtocolError(f"{label} coordinates must be finite")
    return raw  # type: ignore[return-value]


def _normalize_region(
    value: Any,
    label: str,
    *,
    coordinate_space: CoordinateSpace,
    coordinate_order: CoordinateOrder,
    image_width: int | None,
    image_height: int | None,
    raw_merchandise_area: tuple[float, ...] | None,
    line_index: int | None = None,
) -> BoundingRegion:
    try:
        raw = _raw_region(value, label)
        if coordinate_order is CoordinateOrder.LEFT_TOP_RIGHT_BOTTOM:
            left, top, right, bottom = raw
        else:
            top, left, bottom, right = raw
        if coordinate_space is CoordinateSpace.PERCENT:
            left, right = left / 100.0, right / 100.0
            top, bottom = top / 100.0, bottom / 100.0
        elif coordinate_space is CoordinateSpace.PIXELS:
            if image_width is None or image_height is None or image_width <= 0 or image_height <= 0:
                raise CoordinateProtocolError(
                    "pixel coordinates require positive final image dimensions"
                )
            left, right = left / image_width, right / image_width
            top, bottom = top / image_height, bottom / image_height
        region = BoundingRegion(left, top, right, bottom)
        if not region.is_valid():
            raise CoordinateProtocolError(
                f"{label} conflicts with its declared coordinate space or order"
            )
        return region
    except CoordinateProtocolError as exc:
        raise CoordinateProtocolError(
            exc.reason,
            coordinate_space=coordinate_space.value,
            coordinate_order=coordinate_order.value,
            raw_merchandise_area=raw_merchandise_area,
            line_index=line_index,
        ) from exc


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


def _coordinate_protocol(
    data: dict[str, Any],
) -> tuple[CoordinateSpace, CoordinateOrder]:
    try:
        return (
            CoordinateSpace(data["coordinate_space"]),
            CoordinateOrder(data["coordinate_order"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CoordinateProtocolError(
            "receipt coordinate space or order declaration is unknown",
            coordinate_space=(
                str(data.get("coordinate_space"))
                if data.get("coordinate_space") is not None else None
            ),
            coordinate_order=(
                str(data.get("coordinate_order"))
                if data.get("coordinate_order") is not None else None
            ),
        ) from exc


def _pass_region(
    raw: Any,
    label: str,
    *,
    coordinate_space: CoordinateSpace,
    coordinate_order: CoordinateOrder,
    image_width: int | None,
    image_height: int | None,
    source_index: int | None = None,
) -> BoundingRegion:
    return _normalize_region(
        raw,
        label,
        coordinate_space=coordinate_space,
        coordinate_order=coordinate_order,
        image_width=image_width,
        image_height=image_height,
        raw_merchandise_area=None,
        line_index=source_index,
    )


def receipt_layout_from_dict(
    data: Any,
    *,
    image_width: int | None = None,
    image_height: int | None = None,
) -> ReceiptLayoutEvidence:
    """Strict parser for the independent privacy-safe layout pass."""

    if not isinstance(data, dict):
        raise ValueError("receipt layout evidence must be an object")
    fields = (
        "coordinate_space", "coordinate_order", "text_regions",
        "header_regions", "likely_item_total_regions", "boundary_candidates",
        "barriers", "printed_item_count", "printed_item_count_region",
    )
    _strict_keys(data, fields, "receipt layout evidence")
    coordinate_space, coordinate_order = _coordinate_protocol(data)

    def evidence_list(key: str, kind: ReceiptEvidenceKind) -> tuple[ReceiptEvidenceRegion, ...]:
        raw_values = data[key]
        if not isinstance(raw_values, list):
            raise ValueError(f"{key} must be an array")
        result: list[ReceiptEvidenceRegion] = []
        for raw in raw_values:
            if not isinstance(raw, dict):
                raise ValueError(f"{key} entry must be an object")
            _strict_keys(raw, ("source_index", "bounding_region"), f"{key} entry")
            source = raw["source_index"]
            if isinstance(source, bool) or not isinstance(source, int) or source < 0:
                raise ValueError(f"{key} source_index must be non-negative")
            result.append(ReceiptEvidenceRegion(
                kind=kind,
                bounding_region=_pass_region(
                    raw["bounding_region"], key,
                    coordinate_space=coordinate_space,
                    coordinate_order=coordinate_order,
                    image_width=image_width,
                    image_height=image_height,
                    source_index=source,
                ),
                source_index=source,
            ))
        return tuple(result)

    raw_boundaries = data["boundary_candidates"]
    if not isinstance(raw_boundaries, list):
        raise ValueError("boundary_candidates must be an array")
    boundaries: list[ReceiptEndBoundary] = []
    for raw in raw_boundaries:
        if not isinstance(raw, dict):
            raise ValueError("boundary candidate must be an object")
        _strict_keys(raw, ("kind", "source_index", "bounding_region"), "boundary candidate")
        source = raw["source_index"]
        if isinstance(source, bool) or not isinstance(source, int) or source < 0:
            raise ValueError("boundary source_index must be non-negative")
        kind = ReceiptBoundaryKind(raw["kind"])
        if kind is ReceiptBoundaryKind.USER_CONFIRMED:
            raise ValueError("the layout pass cannot report a user-confirmed boundary")
        boundaries.append(ReceiptEndBoundary(
            kind=kind,
            bounding_region=_pass_region(
                raw["bounding_region"], "boundary candidate",
                coordinate_space=coordinate_space,
                coordinate_order=coordinate_order,
                image_width=image_width,
                image_height=image_height,
                source_index=source,
            ),
            source_index=source,
        ))

    raw_barriers = data["barriers"]
    if not isinstance(raw_barriers, list):
        raise ValueError("barriers must be an array")
    barriers: list[ReceiptBarrier] = []
    for raw in raw_barriers:
        if not isinstance(raw, dict):
            raise ValueError("barrier must be an object")
        _strict_keys(raw, ("kind", "source_index", "bounding_region"), "barrier")
        source = raw["source_index"]
        if isinstance(source, bool) or not isinstance(source, int) or source < 0:
            raise ValueError("barrier source_index must be non-negative")
        barriers.append(ReceiptBarrier(
            kind=ReceiptBarrierKind(raw["kind"]),
            bounding_region=_pass_region(
                raw["bounding_region"], "barrier",
                coordinate_space=coordinate_space,
                coordinate_order=coordinate_order,
                image_width=image_width,
                image_height=image_height,
                source_index=source,
            ),
            source_index=source,
        ))

    printed = data["printed_item_count"]
    if printed is not None and (
        isinstance(printed, bool) or not isinstance(printed, int) or printed < 0
    ):
        raise ValueError("printed_item_count must be a non-negative integer or null")
    printed_region = data["printed_item_count_region"]
    if printed_region is not None:
        printed_region = _pass_region(
            printed_region, "printed_item_count_region",
            coordinate_space=coordinate_space,
            coordinate_order=coordinate_order,
            image_width=image_width,
            image_height=image_height,
        )
    return ReceiptLayoutEvidence(
        text_regions=evidence_list("text_regions", ReceiptEvidenceKind.TEXT),
        header_regions=evidence_list("header_regions", ReceiptEvidenceKind.HEADER),
        likely_item_total_regions=evidence_list(
            "likely_item_total_regions", ReceiptEvidenceKind.LIKELY_ITEM_TOTAL
        ),
        boundary_candidates=tuple(boundaries),
        barriers=tuple(barriers),
        printed_item_count=printed,
        printed_item_count_region=printed_region,
    )


def receipt_grouping_from_dict(
    data: Any,
    *,
    image_width: int | None = None,
    image_height: int | None = None,
    layout: ReceiptLayoutEvidence | None = None,
) -> ReceiptFacts:
    """Strict parser for the logical grouping pass.

    The caller supplies layout only after this payload has been independently
    obtained; the grouping request itself never receives layout text/results.
    """

    if not isinstance(data, dict):
        raise ValueError("receipt logical grouping must be an object")
    fields = (
        "store_name", "purchase_date", "currency", "coordinate_space",
        "coordinate_order", "estimated_visible_merchandise_item_count",
        "merchandise_area", "logical_items", "headers",
    )
    _strict_keys(data, fields, "receipt logical grouping")
    coordinate_space, coordinate_order = _coordinate_protocol(data)
    raw_area = _raw_region(data["merchandise_area"], "merchandise_area")
    area = _normalize_region(
        data["merchandise_area"], "merchandise_area",
        coordinate_space=coordinate_space,
        coordinate_order=coordinate_order,
        image_width=image_width,
        image_height=image_height,
        raw_merchandise_area=raw_area,
    )
    estimate = data["estimated_visible_merchandise_item_count"]
    if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
        raise ValueError("estimated item count must be a non-negative integer")

    raw_items = data["logical_items"]
    if not isinstance(raw_items, list):
        raise ValueError("logical_items must be an array")
    logical: list[ReceiptLineFacts] = []
    item_fields = (
        "source_line_index", "bounding_region", "raw_printed_text",
        "generic_item_name", "brand", "language", "form", "quantity",
        "total_weight", "unit_weight", "printed_line_total", "sku", "plu",
        "barcode", "price_region",
    )
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("logical item must be an object")
        _strict_keys(raw, item_fields, "logical item")
        source = raw["source_line_index"]
        if isinstance(source, bool) or not isinstance(source, int) or source < 0:
            raise ValueError("logical item source_line_index must be non-negative")
        try:
            form = FoodForm(raw["form"])
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown logical item form") from exc
        price_region = raw["price_region"]
        logical.append(ReceiptLineFacts(
            source_line_index=source,
            bounding_region=_pass_region(
                raw["bounding_region"], "logical item",
                coordinate_space=coordinate_space,
                coordinate_order=coordinate_order,
                image_width=image_width,
                image_height=image_height,
                source_index=source,
            ),
            raw_printed_text=_required_string(raw["raw_printed_text"], "raw_printed_text"),
            generic_item_name=_required_string(raw["generic_item_name"], "generic_item_name"),
            brand=_optional_string(raw["brand"], "brand"),
            language=_optional_string(raw["language"], "language"),
            form=form,
            quantity=_optional_positive_integer(raw["quantity"], "quantity"),
            total_weight=_parse_weight(raw["total_weight"], "total_weight"),
            unit_weight=_parse_weight(raw["unit_weight"], "unit_weight"),
            printed_line_total=_optional_positive_number(
                raw["printed_line_total"], "printed_line_total"
            ),
            classification=ReceiptLineClassification.MERCHANDISE,
            sku=_optional_string(raw["sku"], "sku"),
            plu=_optional_string(raw["plu"], "plu"),
            barcode=_optional_string(raw["barcode"], "barcode"),
            price_region=(
                _pass_region(
                    price_region, "logical item price_region",
                    coordinate_space=coordinate_space,
                    coordinate_order=coordinate_order,
                    image_width=image_width,
                    image_height=image_height,
                    source_index=source,
                )
                if price_region is not None else None
            ),
        ))

    raw_headers = data["headers"]
    if not isinstance(raw_headers, list):
        raise ValueError("headers must be an array")
    headers: list[ReceiptLineFacts] = []
    for raw in raw_headers:
        if not isinstance(raw, dict):
            raise ValueError("header must be an object")
        _strict_keys(
            raw,
            ("source_line_index", "bounding_region", "raw_printed_text", "language"),
            "header",
        )
        source = raw["source_line_index"]
        if isinstance(source, bool) or not isinstance(source, int) or source < 0:
            raise ValueError("header source_line_index must be non-negative")
        headers.append(ReceiptLineFacts(
            source_line_index=source,
            bounding_region=_pass_region(
                raw["bounding_region"], "header",
                coordinate_space=coordinate_space,
                coordinate_order=coordinate_order,
                image_width=image_width,
                image_height=image_height,
                source_index=source,
            ),
            raw_printed_text=_required_string(raw["raw_printed_text"], "header text"),
            generic_item_name="",
            brand=None,
            language=_optional_string(raw["language"], "header language"),
            form=FoodForm.UNKNOWN,
            quantity=None,
            total_weight=None,
            unit_weight=None,
            printed_line_total=None,
            classification=ReceiptLineClassification.HEADER,
        ))
    return ReceiptFacts(
        store_name=_optional_string(data["store_name"], "store_name"),
        purchase_date=_optional_string(data["purchase_date"], "purchase_date"),
        currency=_optional_string(data["currency"], "currency"),
        estimated_visible_merchandise_line_count=estimate,
        estimated_visible_merchandise_item_count=estimate,
        merchandise_area=area,
        lines=tuple(sorted((*logical, *headers), key=lambda line: line.source_line_index)),
        logical_items=tuple(logical),
        headers=tuple(headers),
        coordinate_space=coordinate_space,
        coordinate_order=coordinate_order,
        raw_merchandise_area=raw_area,
        layout_evidence=layout,
        printed_item_count=(layout.printed_item_count if layout is not None else None),
    )


def receipt_scan_from_dict(data: dict[str, Any]) -> ReceiptScanFacts:
    """Parse the small coordinate-free receipt schema used by the live UI."""

    if not isinstance(data, dict):
        raise ValueError("receipt scan must be an object")
    _strict_keys(
        data,
        ("store_name", "purchase_date", "currency", "unreadable_item_count", "items"),
        "receipt scan",
    )
    unreadable = data["unreadable_item_count"]
    if isinstance(unreadable, bool) or not isinstance(unreadable, int) or unreadable < 0:
        raise ValueError("unreadable_item_count must be a non-negative integer")
    raw_items = data["items"]
    if not isinstance(raw_items, list):
        raise ValueError("receipt scan items must be an array")
    fields = (
        "source_item_index", "raw_printed_text", "generic_item_name", "brand",
        "language", "form", "quantity", "total_weight", "unit_weight",
        "printed_line_total", "kind",
    )
    items: list[ReceiptScanItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("receipt scan item must be an object")
        _strict_keys(raw, fields, "receipt scan item")
        source_index = raw["source_item_index"]
        if isinstance(source_index, bool) or not isinstance(source_index, int):
            raise ValueError("receipt item source index must be an integer")
        try:
            form = FoodForm(raw["form"])
            kind = ReceiptScanItemKind(raw["kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown receipt item form or kind") from exc
        raw_text = _required_string(raw["raw_printed_text"], "raw_printed_text")
        generic_name = _required_string(raw["generic_item_name"], "generic_item_name")
        if kind in (ReceiptScanItemKind.FOOD, ReceiptScanItemKind.UNKNOWN) and not (
            raw_text or generic_name
        ):
            raise ValueError("a food receipt item needs an observable name")
        items.append(ReceiptScanItem(
            source_item_index=source_index,
            raw_printed_text=raw_text,
            generic_item_name=generic_name,
            brand=_optional_string(raw["brand"], "brand"),
            language=_optional_string(raw["language"], "language"),
            form=form,
            quantity=_optional_positive_integer(raw["quantity"], "quantity"),
            total_weight=_parse_weight(raw["total_weight"], "total_weight"),
            unit_weight=_parse_weight(raw["unit_weight"], "unit_weight"),
            printed_line_total=_optional_positive_number(
                raw["printed_line_total"], "printed_line_total"
            ),
            kind=kind,
        ))
    return ReceiptScanFacts(
        store_name=_optional_string(data["store_name"], "store_name"),
        purchase_date=_optional_string(data["purchase_date"], "purchase_date"),
        currency=_optional_string(data["currency"], "currency"),
        unreadable_item_count=unreadable,
        items=tuple(items),
    )


def photo_analysis_from_dict(
    data: Any,
    *,
    image_width: int | None = None,
    image_height: int | None = None,
) -> PhotoAnalysis:
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
            "coordinate_space", "coordinate_order", "bottom_visible", "lines",
        )
        try:
            _strict_keys(raw_receipt, fields, "receipt")
        except ValueError as exc:
            if "coordinate_space" not in raw_receipt or "coordinate_order" not in raw_receipt:
                raise CoordinateProtocolError(
                    "receipt coordinate space and order declarations are required",
                    coordinate_space=raw_receipt.get("coordinate_space"),
                    coordinate_order=raw_receipt.get("coordinate_order"),
                ) from exc
            raise
        try:
            coordinate_space = CoordinateSpace(raw_receipt["coordinate_space"])
            coordinate_order = CoordinateOrder(raw_receipt["coordinate_order"])
        except (TypeError, ValueError) as exc:
            raise CoordinateProtocolError(
                "receipt coordinate space or order declaration is unknown",
                coordinate_space=(
                    str(raw_receipt.get("coordinate_space"))
                    if raw_receipt.get("coordinate_space") is not None else None
                ),
                coordinate_order=(
                    str(raw_receipt.get("coordinate_order"))
                    if raw_receipt.get("coordinate_order") is not None else None
                ),
            ) from exc
        try:
            raw_area = _raw_region(
                raw_receipt["merchandise_area"], "merchandise_area"
            )
        except CoordinateProtocolError as exc:
            raise CoordinateProtocolError(
                exc.reason,
                coordinate_space=coordinate_space.value,
                coordinate_order=coordinate_order.value,
            ) from exc
        merchandise_area = _normalize_region(
            raw_receipt["merchandise_area"],
            "merchandise_area",
            coordinate_space=coordinate_space,
            coordinate_order=coordinate_order,
            image_width=image_width,
            image_height=image_height,
            raw_merchandise_area=raw_area,
        )
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
                bounding_region=_normalize_region(
                    raw_line["bounding_region"],
                    "bounding_region",
                    coordinate_space=coordinate_space,
                    coordinate_order=coordinate_order,
                    image_width=image_width,
                    image_height=image_height,
                    raw_merchandise_area=raw_area,
                    line_index=source_index,
                ),
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
            merchandise_area=merchandise_area,
            bottom_visible=raw_receipt["bottom_visible"],
            lines=tuple(lines),
            coordinate_space=coordinate_space,
            coordinate_order=coordinate_order,
            raw_merchandise_area=raw_area,
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
