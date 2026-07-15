from dataclasses import replace

from models.photo_analysis import (
    BoundingRegion,
    FoodForm,
    ReceiptFacts,
    ReceiptLineClassification,
    ReceiptLineFacts,
)
from services.receipt_validation import (
    combine_receipt_segments,
    flag_intra_segment_duplicates,
    validate_receipt_coverage,
)


def line(index, top, text=None, *, segment=0, classification=None):
    return ReceiptLineFacts(
        source_line_index=index,
        bounding_region=BoundingRegion(0.1, top, 0.9, top + 0.06),
        raw_printed_text=text or f"ITEM {index}",
        generic_item_name="item",
        brand=None,
        language="en",
        form=FoodForm.UNKNOWN,
        quantity=1,
        total_weight=None,
        unit_weight=None,
        printed_line_total=2.0,
        classification=classification or ReceiptLineClassification.MERCHANDISE,
        segment_index=segment,
    )


def receipt(lines, estimate=None, bottom=True, area=None):
    return ReceiptFacts(
        store_name="Store",
        purchase_date="2026-07-14",
        currency="USD",
        estimated_visible_merchandise_line_count=(estimate if estimate is not None else len(lines)),
        merchandise_area=area or BoundingRegion(0.05, 0.1, 0.95, 0.55),
        bottom_visible=bottom,
        lines=tuple(lines),
    )


def test_complete_receipt_passes_all_coverage_gates():
    value = receipt([line(0, 0.18), line(1, 0.30), line(2, 0.45)])
    assert validate_receipt_coverage(value, 600, 1000).complete


def test_line_count_bottom_resolution_order_and_bounds_fail():
    value = receipt(
        [line(2, 0.2)],
        estimate=8,
        area=BoundingRegion(0.05, 0.1, 0.95, 0.9),
    )
    result = validate_receipt_coverage(value, 300, 150)
    assert not result.complete
    assert any("approximately 8" in reason for reason in result.reasons)
    assert any("final visible" in reason for reason in result.reasons)
    assert any("resolution" in reason for reason in result.reasons)


def test_high_iou_flags_later_output_but_keeps_it():
    first = line(0, 0.2)
    later = replace(
        line(1, 0.2),
        bounding_region=BoundingRegion(0.101, 0.201, 0.899, 0.259),
    )
    flagged = flag_intra_segment_duplicates(receipt([first, later], estimate=1))
    assert len(flagged.lines) == 2
    assert not flagged.lines[0].possible_duplicate
    assert flagged.lines[1].possible_duplicate


def test_identical_text_in_different_regions_is_legitimate():
    flagged = flag_intra_segment_duplicates(receipt([
        line(0, 0.2, "MILK 3.00"),
        line(1, 0.4, "MILK 3.00"),
    ]))
    assert not any(item.possible_duplicate for item in flagged.lines)


def test_segment_overlap_needs_text_and_neighbor_context():
    first = receipt([
        line(0, 0.2, "BREAD"),
        line(1, 0.3, "MILK"),
        line(2, 0.4, "EGGS"),
    ])
    second = receipt([
        line(0, 0.2, "MILK"),
        line(1, 0.3, "EGGS"),
        line(2, 0.4, "APPLES"),
    ])
    combined = combine_receipt_segments([first, second])
    second_lines = [item for item in combined.lines if item.segment_index == 1]
    assert second_lines[0].possible_duplicate
    assert second_lines[1].possible_duplicate
    assert not second_lines[2].possible_duplicate

    unrelated = receipt([line(0, 0.2, "MILK"), line(1, 0.3, "SOAP")])
    no_context = combine_receipt_segments([first, unrelated])
    assert not [item for item in no_context.lines if item.segment_index == 1][0].possible_duplicate
