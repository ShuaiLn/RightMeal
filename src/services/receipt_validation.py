"""Local receipt geometry, coverage, and multi-segment review rules.

The layout and logical-grouping model passes are intentionally independent.
This module is the only authority that joins their privacy-safe geometry,
selects a merchandise end boundary, and decides whether automatic confirmation
is safe. A printed item count is diagnostic only and is never read by boundary
selection.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import Sequence

from models.photo_analysis import (
    BoundingRegion,
    CoverageValidation,
    ReceiptBarrier,
    ReceiptBarrierKind,
    ReceiptBoundaryKind,
    ReceiptEndBoundary,
    ReceiptEvidenceRegion,
    ReceiptFacts,
    ReceiptLineClassification,
    ReceiptLineFacts,
)

MAX_MERCHANDISE_LINES_PER_IMAGE = 30
MAX_RECEIPT_SEGMENTS = 5
# Kept as compatibility exports. The canonical validator does not use an 85%
# estimate ratio or a fixed model-count tolerance as proof of completeness.
MIN_LINE_COVERAGE_RATIO = 0.85
MAX_LINE_COUNT_DIFFERENCE = 2
MAX_BOTTOM_GAP = 0.08
MIN_VERTICAL_PIXELS_PER_LINE = 18
DUPLICATE_IOU = 0.85
CONTAINMENT_TOLERANCE = 0.001
BOUNDARY_TOLERANCE_PIXELS = 2.0
MIN_LAST_EVIDENCE_GAP_PIXELS = 12.0


class ReceiptSessionStatus(str, Enum):
    AUTO_CONFIRMABLE = "auto_confirmable"
    CONTINUE_UPLOAD = "continue_upload"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class BoundarySelection:
    boundary: ReceiptEndBoundary | None
    reasons: tuple[str, ...] = ()
    conflict_codes: tuple[str, ...] = ()


def _merchandise(lines: Sequence[ReceiptLineFacts]) -> list[ReceiptLineFacts]:
    return [
        line for line in lines
        if line.classification is ReceiptLineClassification.MERCHANDISE
        and not line.possible_duplicate
    ]


def _logical_items(receipt: ReceiptFacts) -> list[ReceiptLineFacts]:
    return [line for line in receipt.logical_items if not line.possible_duplicate]


def _ordered_evidence(receipt: ReceiptFacts) -> list[ReceiptLineFacts]:
    return sorted(
        (*_logical_items(receipt), *receipt.headers),
        key=lambda line: (line.bounding_region.y1, line.source_line_index),
    )


def flag_intra_segment_duplicates(receipt: ReceiptFacts) -> ReceiptFacts:
    """Flag the later of two highly overlapping logical outputs; never delete it."""

    flagged: list[ReceiptLineFacts] = []
    for line in receipt.lines:
        duplicate = any(
            previous.segment_index == line.segment_index
            and previous.bounding_region.intersection_over_union(line.bounding_region)
            >= DUPLICATE_IOU
            for previous in flagged
        )
        if duplicate:
            line = replace(
                line,
                possible_duplicate=True,
                duplicate_reason="Possible duplicate extraction",
            )
        flagged.append(line)
    logical_ids = {
        (line.segment_index, line.source_line_index)
        for line in receipt.logical_items
    }
    header_ids = {
        (line.segment_index, line.source_line_index)
        for line in receipt.headers
    }
    return replace(
        receipt,
        lines=tuple(flagged),
        logical_items=tuple(
            line for line in flagged
            if (line.segment_index, line.source_line_index) in logical_ids
        ),
        headers=tuple(
            line for line in flagged
            if (line.segment_index, line.source_line_index) in header_ids
        ),
    )


def _barriers_between(
    barriers: Sequence[ReceiptBarrier], top: float, bottom: float
) -> tuple[ReceiptBarrier, ...]:
    return tuple(
        barrier for barrier in barriers
        if barrier.bounding_region.y1 >= top - CONTAINMENT_TOLERANCE
        and barrier.bounding_region.y1 < bottom - CONTAINMENT_TOLERANCE
    )


def select_automatic_end_boundary(
    receipt: ReceiptFacts,
    image_height: int,
) -> BoundarySelection:
    """Select only SUBTOTAL, a strictly qualified TOTAL, or an explicit marker.

    Printed-count coordinates are deliberately absent from this function.
    """

    layout = receipt.layout_evidence
    if layout is None:
        # Legacy wire fixtures are handled by validate_receipt_coverage without
        # inventing a canonical boundary. New analyses always provide layout.
        return BoundarySelection(receipt.merchandise_end_boundary)
    evidence = _ordered_evidence(receipt)
    if not evidence:
        return BoundarySelection(
            None,
            ("No logical merchandise items or headers were grouped.",),
            ("no_logical_evidence",),
        )
    last_bottom = max(line.bounding_region.y2 for line in evidence)
    candidates = sorted(
        layout.boundary_candidates,
        key=lambda candidate: (
            candidate.bounding_region.y1,
            candidate.source_index,
            candidate.kind.value,
        ),
    )
    subtotals = [
        candidate for candidate in candidates
        if candidate.kind is ReceiptBoundaryKind.SUBTOTAL
    ]
    explicit = [
        candidate for candidate in candidates
        if candidate.kind is ReceiptBoundaryKind.EXPLICIT_END_MARKER
    ]
    totals = [
        candidate for candidate in candidates
        if candidate.kind is ReceiptBoundaryKind.TOTAL
    ]
    selected: ReceiptEndBoundary | None = None
    if subtotals:
        selected = subtotals[0]
    elif explicit:
        selected = explicit[0]
    elif totals:
        total = totals[0]
        if total.bounding_region.y1 < last_bottom - CONTAINMENT_TOLERANCE:
            return BoundarySelection(
                None,
                ("TOTAL appears before the final grouped merchandise evidence.",),
                ("unsafe_total_boundary",),
            )
        intervening = _barriers_between(
            layout.barriers, last_bottom, total.bounding_region.y1
        )
        payment_below = any(
            barrier.kind is ReceiptBarrierKind.PAYMENT_TENDER
            and barrier.bounding_region.y1 >= total.bounding_region.y2 - CONTAINMENT_TOLERANCE
            for barrier in layout.barriers
        )
        later_item_total = any(
            evidence.source_index != total.source_index
            and evidence.bounding_region.y1
            >= total.bounding_region.y2 - CONTAINMENT_TOLERANCE
            for evidence in layout.likely_item_total_regions
        )
        # A crop immediately after TOTAL is acceptable when no subsequent
        # merchandise evidence exists. Use a small, resolution-aware distance;
        # never infer a boundary from the crop alone.
        crop_gap_px = max(0.0, 1.0 - total.bounding_region.y2) * max(image_height, 1)
        cropped_after_total = crop_gap_px <= max(
            MIN_LAST_EVIDENCE_GAP_PIXELS,
            total.bounding_region.height * max(image_height, 1) * 1.5,
        )
        if (
            not intervening
            and not later_item_total
            and (payment_below or cropped_after_total)
        ):
            selected = total
        else:
            reason = (
                "TOTAL is separated from the last merchandise item by a summary, "
                "tax, payment, tender, or transaction region."
                if intervening else
                "Merchandise-price evidence appears below TOTAL."
                if later_item_total else
                "TOTAL is not confirmed to be before the payment area."
            )
            return BoundarySelection(None, (reason,), ("unsafe_total_boundary",))
    if selected is None:
        return BoundarySelection(
            None,
            ("No reliable merchandise end boundary is visible.",),
            ("missing_end_boundary",),
        )
    return BoundarySelection(selected)


def _regions_overlap_vertically(
    first: BoundingRegion, second: BoundingRegion, tolerance: float
) -> bool:
    return min(first.y2, second.y2) >= max(first.y1, second.y1) - tolerance


def _assigned_items_for_total(
    region: BoundingRegion,
    items: Sequence[ReceiptLineFacts],
    tolerance: float,
) -> list[int]:
    matches: list[int] = []
    for index, item in enumerate(items):
        price = item.price_region
        if price is not None and (
            price.intersection_over_union(region) > 0
            or _regions_overlap_vertically(price, region, tolerance)
        ):
            matches.append(index)
            continue
        if _regions_overlap_vertically(item.bounding_region, region, tolerance):
            matches.append(index)
    return matches


def _unassigned_text_bands(
    regions: Sequence[ReceiptEvidenceRegion],
    grouped: Sequence[ReceiptLineFacts],
    area: BoundingRegion,
    tolerance: float,
) -> list[ReceiptEvidenceRegion]:
    result = []
    for evidence in regions:
        if not area.contains(evidence.bounding_region, tolerance=tolerance):
            continue
        if not any(
            _regions_overlap_vertically(
                evidence.bounding_region, line.bounding_region, tolerance
            )
            for line in grouped
        ):
            result.append(evidence)
    return result


def _legacy_validate(
    receipt: ReceiptFacts,
    image_height: int,
) -> CoverageValidation:
    """Compatibility for old one-pass fixtures/saved analyses only.

    New analyses always have ``layout_evidence`` and cannot use this path.
    """

    reasons: list[str] = []
    estimate = receipt.estimated_visible_merchandise_line_count
    items = _merchandise(receipt.lines)
    if estimate <= 0:
        reasons.append("No visible merchandise lines were estimated.")
    if estimate > MAX_MERCHANDISE_LINES_PER_IMAGE:
        reasons.append(
            "This photo appears to contain more than 30 merchandise lines; "
            "use 2-5 overlapping segment photos."
        )
    count = len(items)
    minimum = math.ceil(estimate * MIN_LINE_COVERAGE_RATIO)
    if count < minimum or abs(count - estimate) > MAX_LINE_COUNT_DIFFERENCE:
        reasons.append(
            f"Only {count} of approximately {estimate} visible merchandise lines "
            "were extracted."
        )
    if receipt.bottom_visible and items:
        gap = receipt.merchandise_area.y2 - items[-1].bounding_region.y2
        if gap > MAX_BOTTOM_GAP:
            reasons.append("The final visible merchandise lines may be missing.")
    if estimate > 0 and receipt.merchandise_area.height * image_height < (
        estimate * MIN_VERTICAL_PIXELS_PER_LINE
    ):
        reasons.append("The receipt image resolution is too low for reliable coverage.")
    return CoverageValidation(
        complete=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        failure_stage="coverage" if reasons else None,
    )


def validate_receipt_coverage(
    receipt: ReceiptFacts,
    image_width: int,
    image_height: int,
    *,
    ink_density_bands: Sequence[tuple[float, float]] = (),
) -> CoverageValidation:
    """Join independent evidence and apply the canonical reliability gates."""

    if image_width <= 0 or image_height <= 0:
        return CoverageValidation(
            False,
            ("The image dimensions are invalid.",),
            "image_dimensions",
            strong_conflict=True,
            retry_passes=("layout", "grouping"),
            conflict_codes=("invalid_image_dimensions",),
        )

    layout = receipt.layout_evidence
    coordinate_regions: list[tuple[str, int | None, BoundingRegion]] = [
        ("merchandise area", None, receipt.merchandise_area)
    ]
    for line in (*receipt.logical_items, *receipt.headers):
        coordinate_regions.append(("line", line.source_line_index, line.bounding_region))
        if line.price_region is not None:
            coordinate_regions.append(("price", line.source_line_index, line.price_region))
    if layout is not None:
        for evidence in (
            *layout.text_regions,
            *layout.header_regions,
            *layout.likely_item_total_regions,
        ):
            coordinate_regions.append(("layout", evidence.source_index, evidence.bounding_region))
        for boundary in layout.boundary_candidates:
            coordinate_regions.append(("boundary", boundary.source_index, boundary.bounding_region))
        for barrier in layout.barriers:
            coordinate_regions.append(("barrier", barrier.source_index, barrier.bounding_region))
    invalid = [entry for entry in coordinate_regions if not entry[2].is_valid()]
    if invalid:
        label, index, _ = invalid[0]
        reason = (
            f"Line {index} has invalid coordinates."
            if index is not None else f"The reported {label} is invalid."
        )
        return CoverageValidation(
            False,
            (reason,),
            "coordinates",
            strong_conflict=True,
            retry_passes=("layout", "grouping"),
            conflict_codes=("invalid_coordinates",),
        )

    # Preserve support for old persisted/test payloads without letting that
    # wire-only path influence new canonical analyses.
    if layout is None and receipt.merchandise_end_boundary is None:
        structure = _validate_containment_and_order(receipt)
        if structure is not None:
            return structure
        return _legacy_validate(receipt, image_height)

    structure = _validate_containment_and_order(receipt)
    if structure is not None:
        return structure

    selection = (
        BoundarySelection(receipt.user_confirmed_boundary)
        if receipt.user_confirmed_boundary is not None
        else select_automatic_end_boundary(receipt, image_height)
    )
    if selection.boundary is None:
        return CoverageValidation(
            False,
            selection.reasons,
            "boundary",
            strong_conflict=("missing_end_boundary" not in selection.conflict_codes),
            retry_passes=("layout",),
            conflict_codes=selection.conflict_codes,
        )
    boundary = selection.boundary
    tolerance = BOUNDARY_TOLERANCE_PIXELS / image_height
    evidence = _ordered_evidence(receipt)
    last_bottom = max((line.bounding_region.y2 for line in evidence), default=0.0)
    reasons: list[str] = []
    codes: list[str] = []
    retry: set[str] = set()
    if last_bottom > receipt.merchandise_area.y2 + tolerance:
        reasons.append("The final merchandise/header evidence extends below its area.")
        codes.append("evidence_crosses_area")
        retry.update(("layout", "grouping"))
    if receipt.merchandise_area.y2 > boundary.bounding_region.y1 + tolerance:
        reasons.append("The merchandise area crosses its selected end boundary.")
        codes.append("area_crosses_boundary")
        retry.update(("layout", "grouping"))
    if layout is not None:
        earlier_boundaries = [
            candidate
            for candidate in layout.boundary_candidates
            if candidate != boundary
            and candidate.bounding_region.y1 < boundary.bounding_region.y1
        ]
        if any(
            receipt.merchandise_area.y2
            > candidate.bounding_region.y1 + tolerance
            for candidate in earlier_boundaries
        ):
            reasons.append(
                "The merchandise area crosses an earlier subtotal, total, or "
                "explicit end boundary."
            )
            codes.append("area_crosses_earlier_boundary")
            retry.add("layout")
        earlier_barriers = [
            barrier for barrier in layout.barriers
            if barrier.bounding_region.y1 < boundary.bounding_region.y1
            and barrier.bounding_region.y1 >= last_bottom - tolerance
        ]
        if any(
            receipt.merchandise_area.y2 > barrier.bounding_region.y1 + tolerance
            for barrier in earlier_barriers
        ):
            reasons.append(
                "The merchandise area crosses an earlier tax, summary, payment, "
                "tender, or transaction barrier."
            )
            codes.append("area_crosses_barrier")
            retry.add("layout")
    if reasons:
        return CoverageValidation(
            False,
            tuple(dict.fromkeys(reasons)),
            "boundary",
            strong_conflict=True,
            retry_passes=tuple(sorted(retry)),
            conflict_codes=tuple(dict.fromkeys(codes)),
        )

    assert layout is not None or receipt.user_confirmed_boundary is not None
    weak_reasons: list[str] = []
    weak_codes: list[str] = []
    if layout is not None:
        items = _logical_items(receipt)
        owner_counts: dict[int, int] = {}
        unassigned_totals = 0
        for total in layout.likely_item_total_regions:
            owners = _assigned_items_for_total(total.bounding_region, items, tolerance)
            if len(owners) != 1:
                unassigned_totals += 1
            else:
                owner_counts[owners[0]] = owner_counts.get(owners[0], 0) + 1
        if unassigned_totals or any(count != 1 for count in owner_counts.values()):
            return CoverageValidation(
                False,
                ("One or more visible item-total regions could not be assigned uniquely.",),
                "item_total_assignment",
                strong_conflict=True,
                retry_passes=("layout", "grouping"),
                conflict_codes=("unassigned_item_total",),
            )

        grouped = (*items, *receipt.headers)
        unassigned_text = _unassigned_text_bands(
            (*layout.text_regions, *layout.header_regions),
            grouped,
            receipt.merchandise_area,
            tolerance,
        )
        if unassigned_text:
            return CoverageValidation(
                False,
                ("Visible merchandise text/header evidence was not logically grouped.",),
                "evidence_gap",
                strong_conflict=True,
                retry_passes=("grouping",),
                conflict_codes=("ungrouped_evidence_band",),
            )

        assigned_regions = [
            line.bounding_region for line in grouped
        ] + [region.bounding_region for region in layout.likely_item_total_regions]
        if assigned_regions:
            heights = [region.height * image_height for region in assigned_regions]
            median_height = statistics.median(heights)
            final_bottom = max(region.y2 for region in assigned_regions)
            gap_px = max(0.0, receipt.merchandise_area.y2 - final_bottom) * image_height
            if gap_px > max(MIN_LAST_EVIDENCE_GAP_PIXELS, 1.5 * median_height):
                return CoverageValidation(
                    False,
                    ("The final assigned evidence is too far above the merchandise boundary.",),
                    "evidence_gap",
                    strong_conflict=True,
                    retry_passes=("layout", "grouping"),
                    conflict_codes=("strong_final_evidence_gap",),
                )

        printed = receipt.printed_item_count
        if printed is not None and printed > 0:
            tolerance_count = max(2, math.ceil(printed * 0.20))
            logical_count = len(items)
            quantity_total = sum(line.quantity or 1 for line in items)
            logical_ok = abs(printed - logical_count) <= tolerance_count
            quantity_ok = abs(printed - quantity_total) <= tolerance_count
            if not (logical_ok or quantity_ok):
                weak_reasons.append(
                    "The printed item count does not agree with the logical items or quantities."
                )
                weak_codes.append("printed_count_mismatch")

    # Ink bands are deliberately weak: they can request review but can neither
    # prove completeness nor block an otherwise coherent receipt by themselves.
    if ink_density_bands and evidence:
        unexplained = [
            band for band in ink_density_bands
            if receipt.merchandise_area.y1 <= band[0] <= receipt.merchandise_area.y2
            and not any(
                _regions_overlap_vertically(
                    BoundingRegion(receipt.merchandise_area.x1, band[0], receipt.merchandise_area.x2, band[1]),
                    line.bounding_region,
                    tolerance,
                )
                for line in evidence
            )
        ]
        if unexplained:
            weak_reasons.append("Some faint receipt bands need manual review.")
            weak_codes.append("weak_ink_gap")

    if weak_reasons:
        return CoverageValidation(
            False,
            tuple(dict.fromkeys(weak_reasons)),
            "manual_review",
            manual_review_required=True,
            strong_conflict=False,
            retry_passes=("grouping",),
            conflict_codes=tuple(dict.fromkeys(weak_codes)),
        )

    return CoverageValidation(True)


def _validate_containment_and_order(receipt: ReceiptFacts) -> CoverageValidation | None:
    reasons: list[str] = []
    # Source order is the grouping pass's asserted reading order. Validate its
    # geometry rather than sorting the geometry into an order that would hide a
    # crossing.
    all_evidence = (*receipt.logical_items, *receipt.headers)
    source_keys = [
        (line.segment_index, line.source_line_index)
        for line in all_evidence
    ]
    if len(set(source_keys)) != len(source_keys):
        return CoverageValidation(
            False,
            ("Receipt items and headers must use unique source indices per segment.",),
            "containment_order",
            strong_conflict=True,
            retry_passes=("grouping",),
            conflict_codes=("duplicate_source_index",),
        )
    evidence = sorted(
        (*_logical_items(receipt), *receipt.headers),
        key=lambda line: line.source_line_index,
    )
    previous_top = -1.0
    previous_index = -1
    for line in evidence:
        if not receipt.merchandise_area.contains(
            line.bounding_region, tolerance=CONTAINMENT_TOLERANCE
        ):
            reasons.append(
                f"Line {line.source_line_index} lies outside the merchandise area."
            )
        if (
            line.bounding_region.y1 < previous_top - CONTAINMENT_TOLERANCE
            or (
                abs(line.bounding_region.y1 - previous_top) <= CONTAINMENT_TOLERANCE
                and line.source_line_index <= previous_index
            )
        ):
            reasons.append("Receipt items and headers are not ordered from top to bottom.")
            break
        previous_top = line.bounding_region.y1
        previous_index = line.source_line_index
    if not reasons:
        return None
    return CoverageValidation(
        False,
        tuple(dict.fromkeys(reasons)),
        "containment_order",
        strong_conflict=True,
        retry_passes=("grouping",),
        conflict_codes=("containment_or_order",),
    )


def confirm_manual_boundary(
    receipt: ReceiptFacts,
    *,
    last_item_source_index: int,
    boundary_y: float,
) -> ReceiptFacts:
    """Validate and record an explicit user merchandise-end line."""

    items = _logical_items(receipt)
    matching_items = [
        item for item in items
        if item.source_line_index == last_item_source_index
    ]
    if not matching_items:
        raise ValueError("Select a logical merchandise item as the final item.")
    if len(matching_items) != 1:
        raise ValueError(
            "Receipt item source indices are ambiguous; retry the analysis."
        )
    selected = matching_items[0]
    if not math.isfinite(boundary_y) or not 0.0 < boundary_y < 1.0:
        raise ValueError("The manual boundary must be inside the image.")
    if boundary_y <= selected.bounding_region.y2 + CONTAINMENT_TOLERANCE:
        raise ValueError("The manual boundary must be after the selected final item.")
    later_items = [
        item for item in items
        if item.source_line_index != selected.source_line_index
        and item.bounding_region.y1 < boundary_y
        and item.bounding_region.y1 > selected.bounding_region.y1
    ]
    if later_items:
        raise ValueError("The selected item is not the final item above this boundary.")
    barriers = receipt.layout_evidence.barriers if receipt.layout_evidence else ()
    if any(
        barrier.kind in (
            ReceiptBarrierKind.PAYMENT_TENDER,
            ReceiptBarrierKind.TRANSACTION,
        )
        and barrier.bounding_region.y1 <= boundary_y
        for barrier in barriers
    ):
        raise ValueError("The manual boundary crosses a payment or transaction barrier.")
    boundary = ReceiptEndBoundary(
        kind=ReceiptBoundaryKind.USER_CONFIRMED,
        bounding_region=BoundingRegion(
            receipt.merchandise_area.x1,
            boundary_y,
            receipt.merchandise_area.x2,
            min(1.0, boundary_y + 0.001),
        ),
        source_index=last_item_source_index,
    )
    area = BoundingRegion(
        receipt.merchandise_area.x1,
        receipt.merchandise_area.y1,
        receipt.merchandise_area.x2,
        boundary_y,
    )
    return replace(
        receipt,
        merchandise_area=area,
        user_confirmed_boundary=boundary,
        merchandise_end_boundary=None,
    )


def rebase_receipt_to_boundary_crop(
    receipt: ReceiptFacts,
    boundary_y: float,
) -> ReceiptFacts:
    """Rebase validated geometry after cropping the image at a manual line.

    Payment/transaction regions below the line are removed with the pixels.
    The user boundary remains explicitly user-confirmed at the new image edge.
    """

    if receipt.user_confirmed_boundary is None:
        raise ValueError("A receipt must have a confirmed manual boundary before cropping.")
    if not math.isfinite(boundary_y) or not 0 < boundary_y <= 1:
        raise ValueError("The crop boundary is outside the image.")

    def region(value: BoundingRegion) -> BoundingRegion:
        if value.y1 >= boundary_y:
            raise ValueError("A retained receipt region starts below the crop boundary.")
        return BoundingRegion(
            value.x1,
            min(1.0, value.y1 / boundary_y),
            value.x2,
            min(1.0, value.y2 / boundary_y),
        )

    def line(value: ReceiptLineFacts) -> ReceiptLineFacts:
        return replace(
            value,
            bounding_region=region(value.bounding_region),
            price_region=(region(value.price_region) if value.price_region else None),
        )

    logical = tuple(line(value) for value in receipt.logical_items)
    headers = tuple(line(value) for value in receipt.headers)
    layout = receipt.layout_evidence
    if layout is not None:
        def evidences(values):
            return tuple(
                replace(value, bounding_region=region(value.bounding_region))
                for value in values if value.bounding_region.y1 < boundary_y
            )

        layout = replace(
            layout,
            text_regions=evidences(layout.text_regions),
            header_regions=evidences(layout.header_regions),
            likely_item_total_regions=evidences(layout.likely_item_total_regions),
            boundary_candidates=(),
            barriers=tuple(
                replace(value, bounding_region=region(value.bounding_region))
                for value in layout.barriers if value.bounding_region.y1 < boundary_y
            ),
            printed_item_count_region=(
                region(layout.printed_item_count_region)
                if layout.printed_item_count_region is not None
                and layout.printed_item_count_region.y1 < boundary_y
                else None
            ),
        )
    epsilon = min(0.001, 1.0 / max(1000.0, boundary_y * 1000.0))
    manual = replace(
        receipt.user_confirmed_boundary,
        bounding_region=BoundingRegion(
            receipt.user_confirmed_boundary.bounding_region.x1,
            1.0 - epsilon,
            receipt.user_confirmed_boundary.bounding_region.x2,
            1.0,
        ),
    )
    area = receipt.merchandise_area
    rebased_area = BoundingRegion(
        area.x1,
        area.y1 / boundary_y,
        area.x2,
        1.0 - epsilon,
    )
    return replace(
        receipt,
        merchandise_area=rebased_area,
        lines=tuple(sorted((*logical, *headers), key=lambda value: value.source_line_index)),
        logical_items=logical,
        headers=headers,
        layout_evidence=layout,
        user_confirmed_boundary=manual,
        merchandise_end_boundary=None,
    )


def _normalized_line(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _neighbor_context(lines: Sequence[ReceiptLineFacts], index: int) -> set[str]:
    context: set[str] = set()
    for neighbor in (index - 1, index + 1):
        if 0 <= neighbor < len(lines):
            value = _normalized_line(lines[neighbor].raw_printed_text)
            if value:
                context.add(value)
    return context


def combine_receipt_segments(segments: Sequence[ReceiptFacts]) -> ReceiptFacts:
    """Combine segment output while retaining explicit segment identity."""

    if not segments:
        raise ValueError("At least one receipt segment is required.")
    if len(segments) > MAX_RECEIPT_SEGMENTS:
        raise ValueError("A receipt session supports at most five segments.")
    combined: list[ReceiptLineFacts] = []
    for segment_index, segment in enumerate(segments):
        current = [replace(line, segment_index=segment_index) for line in segment.lines]
        if segment_index:
            previous = list(segments[segment_index - 1].lines)
            previous_by_text: dict[str, list[int]] = {}
            for index, line in enumerate(previous):
                previous_by_text.setdefault(_normalized_line(line.raw_printed_text), []).append(index)
            for index, line in enumerate(current):
                text = _normalized_line(line.raw_printed_text)
                for previous_index in previous_by_text.get(text, []):
                    if _neighbor_context(current, index) & _neighbor_context(previous, previous_index):
                        current[index] = replace(
                            line,
                            possible_duplicate=True,
                            duplicate_reason="Possible overlapping receipt segment",
                        )
                        break
        combined.extend(current)

    first = segments[0]
    last = segments[-1]
    logical = tuple(
        line for line in combined
        if line.classification is ReceiptLineClassification.MERCHANDISE
    )
    headers = tuple(
        line for line in combined
        if line.classification is ReceiptLineClassification.HEADER
    )
    return ReceiptFacts(
        store_name=first.store_name or last.store_name,
        purchase_date=first.purchase_date or last.purchase_date,
        currency=first.currency or last.currency,
        estimated_visible_merchandise_line_count=sum(
            segment.estimated_visible_merchandise_line_count for segment in segments
        ),
        estimated_visible_merchandise_item_count=sum(
            segment.estimated_visible_merchandise_item_count or 0
            for segment in segments
        ),
        merchandise_area=last.merchandise_area,
        bottom_visible=last.bottom_visible,
        lines=tuple(combined),
        logical_items=logical,
        headers=headers,
        coordinate_space=first.coordinate_space,
        coordinate_order=first.coordinate_order,
        raw_merchandise_area=first.raw_merchandise_area,
        merchandise_end_boundary=last.merchandise_end_boundary,
        user_confirmed_boundary=last.user_confirmed_boundary,
        printed_item_count=last.printed_item_count,
        layout_evidence=last.layout_evidence,
    )


def receipt_session_status(segments: Sequence[ReceiptFacts]) -> ReceiptSessionStatus:
    if not segments or len(segments) > MAX_RECEIPT_SEGMENTS:
        return ReceiptSessionStatus.BLOCKED
    last = segments[-1]
    if (
        last.merchandise_end_boundary is not None
        or last.user_confirmed_boundary is not None
        or (last.layout_evidence is None and last.bottom_visible)
    ):
        return ReceiptSessionStatus.AUTO_CONFIRMABLE
    if len(segments) < MAX_RECEIPT_SEGMENTS:
        return ReceiptSessionStatus.CONTINUE_UPLOAD
    return ReceiptSessionStatus.MANUAL_REVIEW_REQUIRED


def receipt_session_confirmable(segments: Sequence[ReceiptFacts]) -> bool:
    return receipt_session_status(segments) is ReceiptSessionStatus.AUTO_CONFIRMABLE


__all__ = [
    "BoundarySelection", "ReceiptSessionStatus", "combine_receipt_segments",
    "confirm_manual_boundary", "flag_intra_segment_duplicates",
    "rebase_receipt_to_boundary_crop",
    "receipt_session_confirmable", "receipt_session_status",
    "select_automatic_end_boundary", "validate_receipt_coverage",
]
