"""Local receipt coverage checks and duplicate-extraction detection."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Sequence

from models.photo_analysis import (
    CoverageValidation,
    ReceiptFacts,
    ReceiptLineClassification,
    ReceiptLineFacts,
)

MAX_MERCHANDISE_LINES_PER_IMAGE = 30
MIN_LINE_COVERAGE_RATIO = 0.85
MAX_LINE_COUNT_DIFFERENCE = 2
MAX_BOTTOM_GAP = 0.08
MIN_VERTICAL_PIXELS_PER_LINE = 18
DUPLICATE_IOU = 0.85


def _merchandise(lines: Sequence[ReceiptLineFacts]) -> list[ReceiptLineFacts]:
    return [
        line for line in lines
        if line.classification is ReceiptLineClassification.MERCHANDISE
        and not line.possible_duplicate
    ]


def flag_intra_segment_duplicates(receipt: ReceiptFacts) -> ReceiptFacts:
    """Flag the later of two highly overlapping model outputs; never delete it."""

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
    return replace(receipt, lines=tuple(flagged))


def validate_receipt_coverage(
    receipt: ReceiptFacts,
    image_width: int,
    image_height: int,
) -> CoverageValidation:
    """Apply all reliability gates before a receipt can be confirmed."""

    reasons: list[str] = []
    estimate = receipt.estimated_visible_merchandise_line_count
    if image_width <= 0 or image_height <= 0:
        reasons.append("The image dimensions are invalid.")
    if estimate <= 0:
        reasons.append("No visible merchandise lines were estimated.")
    if estimate > MAX_MERCHANDISE_LINES_PER_IMAGE:
        reasons.append(
            "This photo appears to contain more than 30 merchandise lines; "
            "use 2-3 overlapping segment photos."
        )
    if not receipt.merchandise_area.is_valid():
        reasons.append("The reported merchandise area is invalid.")

    previous_index = -1
    previous_top = -1.0
    for line in receipt.lines:
        region = line.bounding_region
        if not region.is_valid():
            reasons.append(f"Line {line.source_line_index} has invalid coordinates.")
        elif (
            receipt.merchandise_area.is_valid()
            and not receipt.merchandise_area.contains(region)
        ):
            reasons.append(
                f"Line {line.source_line_index} lies outside the merchandise area."
            )
        if line.source_line_index <= previous_index or region.y1 < previous_top:
            reasons.append("Receipt lines are not ordered from top to bottom.")
            break
        previous_index = line.source_line_index
        previous_top = region.y1

    items = _merchandise(receipt.lines)
    count = len(items)
    minimum = math.ceil(estimate * MIN_LINE_COVERAGE_RATIO)
    if count < minimum or abs(count - estimate) > MAX_LINE_COUNT_DIFFERENCE:
        reasons.append(
            f"Only {count} of approximately {estimate} visible merchandise lines "
            "were extracted."
        )
    if receipt.bottom_visible and items:
        bottom_gap = receipt.merchandise_area.y2 - items[-1].bounding_region.y2
        if bottom_gap > MAX_BOTTOM_GAP:
            reasons.append("The final visible merchandise lines may be missing.")
    if estimate > 0 and receipt.merchandise_area.is_valid():
        vertical_pixels = receipt.merchandise_area.height * image_height
        if vertical_pixels < estimate * MIN_VERTICAL_PIXELS_PER_LINE:
            reasons.append("The receipt image resolution is too low for reliable coverage.")
    return CoverageValidation(complete=not reasons, reasons=tuple(dict.fromkeys(reasons)))


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
    """Combine validated segment output and flag possible overlap duplicates.

    Equal text alone is not enough: a neighboring line must also agree, so two
    legitimate repeated purchases in different source regions remain separate.
    """

    if not segments:
        raise ValueError("At least one receipt segment is required.")
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
    # The aggregate estimate intentionally includes all segment estimates. The
    # individual segment validations are the coverage authority; overlap lines
    # stay visible and user-controlled in this combined confirmation result.
    return ReceiptFacts(
        store_name=first.store_name or last.store_name,
        purchase_date=first.purchase_date or last.purchase_date,
        currency=first.currency or last.currency,
        estimated_visible_merchandise_line_count=sum(
            segment.estimated_visible_merchandise_line_count for segment in segments
        ),
        merchandise_area=first.merchandise_area,
        bottom_visible=last.bottom_visible,
        lines=tuple(combined),
    )
