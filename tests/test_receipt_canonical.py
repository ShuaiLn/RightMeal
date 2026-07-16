"""Canonical two-pass receipt boundary and coverage rules."""

from dataclasses import replace

import pytest

from models.photo_analysis import (
    BoundingRegion,
    FoodForm,
    ReceiptBarrier,
    ReceiptBarrierKind,
    ReceiptBoundaryKind,
    ReceiptEndBoundary,
    ReceiptEvidenceKind,
    ReceiptEvidenceRegion,
    ReceiptFacts,
    ReceiptLayoutEvidence,
    ReceiptLineClassification,
    ReceiptLineFacts,
)
from services.receipt_validation import (
    ReceiptSessionStatus,
    confirm_manual_boundary,
    receipt_session_status,
    rebase_receipt_to_boundary_crop,
    select_automatic_end_boundary,
    validate_receipt_coverage,
)


def item(index: int, top: float, quantity: int = 1) -> ReceiptLineFacts:
    region = BoundingRegion(0.08, top, 0.92, top + 0.06)
    return ReceiptLineFacts(
        source_line_index=index,
        bounding_region=region,
        raw_printed_text=f"ITEM {index}",
        generic_item_name=f"item {index}",
        brand=None,
        language="en",
        form=FoodForm.UNKNOWN,
        quantity=quantity,
        total_weight=None,
        unit_weight=None,
        printed_line_total=2.0,
        classification=ReceiptLineClassification.MERCHANDISE,
        price_region=BoundingRegion(0.75, top + 0.01, 0.91, top + 0.05),
    )


def canonical(
    items: tuple[ReceiptLineFacts, ...],
    *,
    boundary_kind: ReceiptBoundaryKind | None = ReceiptBoundaryKind.SUBTOTAL,
    boundary_top: float = 0.9,
    area_bottom: float | None = None,
    barriers: tuple[ReceiptBarrier, ...] = (),
    printed_count: int | None = None,
    headers: tuple[ReceiptLineFacts, ...] = (),
) -> ReceiptFacts:
    last_bottom = max(
        (entry.bounding_region.y2 for entry in (*items, *headers)), default=0.2
    )
    area_bottom = last_bottom if area_bottom is None else area_bottom
    totals = tuple(
        ReceiptEvidenceRegion(
            ReceiptEvidenceKind.LIKELY_ITEM_TOTAL,
            entry.price_region,
            entry.source_line_index,
        )
        for entry in items
        if entry.price_region is not None
    )
    boundaries = (
        (
            ReceiptEndBoundary(
                boundary_kind,
                BoundingRegion(0.05, boundary_top, 0.95, boundary_top + 0.02),
                100,
            ),
        )
        if boundary_kind is not None else ()
    )
    layout = ReceiptLayoutEvidence(
        likely_item_total_regions=totals,
        boundary_candidates=boundaries,
        barriers=barriers,
        printed_item_count=printed_count,
        # Deliberately place this below payment in several tests. It must never
        # enter geometry selection.
        printed_item_count_region=(
            BoundingRegion(0.1, 0.97, 0.4, 0.99)
            if printed_count is not None else None
        ),
    )
    return ReceiptFacts(
        store_name="Store",
        purchase_date="2026-07-14",
        currency="USD",
        estimated_visible_merchandise_line_count=len(items),
        estimated_visible_merchandise_item_count=len(items),
        merchandise_area=BoundingRegion(0.05, 0.05, 0.95, area_bottom),
        logical_items=items,
        headers=headers,
        layout_evidence=layout,
        printed_item_count=printed_count,
    )


def test_subtotal_is_outside_area_and_can_close_merchandise():
    receipt = canonical((item(0, 0.2), item(1, 0.35)), boundary_top=0.43)
    selected = select_automatic_end_boundary(receipt, 1000)
    assert selected.boundary is not None
    receipt = replace(receipt, merchandise_end_boundary=selected.boundary)
    assert validate_receipt_coverage(receipt, 600, 1000).complete


def test_area_may_not_cross_subtotal_or_earlier_privacy_barrier():
    barrier = ReceiptBarrier(
        ReceiptBarrierKind.TAX_SUMMARY,
        BoundingRegion(0.05, 0.44, 0.95, 0.47),
        50,
    )
    receipt = canonical(
        (item(0, 0.2), item(1, 0.35)),
        boundary_top=0.52,
        area_bottom=0.50,
        barriers=(barrier,),
    )
    selected = select_automatic_end_boundary(receipt, 1000)
    receipt = replace(receipt, merchandise_end_boundary=selected.boundary)
    result = validate_receipt_coverage(receipt, 600, 1000)
    assert result.strong_conflict
    assert "area_crosses_barrier" in result.conflict_codes


def test_later_total_cannot_hide_an_earlier_crossed_subtotal():
    receipt = canonical(
        (item(0, 0.20), item(1, 0.48)),
        boundary_kind=ReceiptBoundaryKind.SUBTOTAL,
        boundary_top=0.43,
        area_bottom=0.55,
    )
    later_total = ReceiptEndBoundary(
        ReceiptBoundaryKind.TOTAL,
        BoundingRegion(0.05, 0.70, 0.95, 0.73),
        101,
    )
    receipt = replace(
        receipt,
        layout_evidence=replace(
            receipt.layout_evidence,
            boundary_candidates=(
                *receipt.layout_evidence.boundary_candidates,
                later_total,
            ),
        ),
    )

    selected = select_automatic_end_boundary(receipt, 1000)
    assert selected.boundary is not None
    assert selected.boundary.kind is ReceiptBoundaryKind.SUBTOTAL
    result = validate_receipt_coverage(
        replace(receipt, merchandise_end_boundary=selected.boundary),
        600,
        1000,
    )
    assert result.strong_conflict
    assert "area_crosses_boundary" in result.conflict_codes


def test_total_requires_no_intervening_summary_and_payment_below():
    payment = ReceiptBarrier(
        ReceiptBarrierKind.PAYMENT_TENDER,
        BoundingRegion(0.05, 0.60, 0.95, 0.70),
        60,
    )
    safe = canonical(
        (item(0, 0.2),),
        boundary_kind=ReceiptBoundaryKind.TOTAL,
        boundary_top=0.50,
        barriers=(payment,),
    )
    assert select_automatic_end_boundary(safe, 1000).boundary is not None

    tax = ReceiptBarrier(
        ReceiptBarrierKind.TAX_SUMMARY,
        BoundingRegion(0.05, 0.42, 0.95, 0.46),
        55,
    )
    unsafe = replace(
        safe,
        layout_evidence=replace(safe.layout_evidence, barriers=(tax, payment)),
    )
    selection = select_automatic_end_boundary(unsafe, 1000)
    assert selection.boundary is None
    assert "unsafe_total_boundary" in selection.conflict_codes


def test_total_is_rejected_when_layout_finds_later_item_price_evidence():
    payment = ReceiptBarrier(
        ReceiptBarrierKind.PAYMENT_TENDER,
        BoundingRegion(0.05, 0.80, 0.95, 0.88),
        80,
    )
    receipt = canonical(
        (item(0, 0.20),),
        boundary_kind=ReceiptBoundaryKind.TOTAL,
        boundary_top=0.50,
        barriers=(payment,),
    )
    later_price = ReceiptEvidenceRegion(
        ReceiptEvidenceKind.LIKELY_ITEM_TOTAL,
        BoundingRegion(0.76, 0.62, 0.91, 0.66),
        77,
    )
    receipt = replace(
        receipt,
        layout_evidence=replace(
            receipt.layout_evidence,
            likely_item_total_regions=(
                *receipt.layout_evidence.likely_item_total_regions,
                later_price,
            ),
        ),
    )

    selection = select_automatic_end_boundary(receipt, 1000)
    assert selection.boundary is None
    assert selection.conflict_codes == ("unsafe_total_boundary",)


def test_printed_count_never_becomes_a_boundary():
    receipt = canonical(
        (item(0, 0.2),), boundary_kind=None, printed_count=1
    )
    selection = select_automatic_end_boundary(receipt, 1000)
    assert selection.boundary is None
    assert selection.conflict_codes == ("missing_end_boundary",)


def test_duplicate_source_indices_retry_grouping_and_block_manual_selection():
    receipt = canonical((item(0, 0.20), item(0, 0.35)), boundary_top=0.45)
    selected = select_automatic_end_boundary(receipt, 1000)
    assert selected.boundary is not None
    receipt = replace(receipt, merchandise_end_boundary=selected.boundary)

    result = validate_receipt_coverage(receipt, 600, 1000)

    assert result.strong_conflict
    assert result.retry_passes == ("grouping",)
    assert result.conflict_codes == ("duplicate_source_index",)
    with pytest.raises(ValueError, match="ambiguous"):
        confirm_manual_boundary(
            receipt,
            last_item_source_index=0,
            boundary_y=0.44,
        )


def test_independent_item_totals_expose_missing_logical_grouping():
    five = tuple(item(index, 0.08 + index * 0.10) for index in range(5))
    receipt = canonical(five, boundary_top=0.68)
    extra = tuple(
        ReceiptEvidenceRegion(
            ReceiptEvidenceKind.LIKELY_ITEM_TOTAL,
            BoundingRegion(0.75, 0.70 + index * 0.02, 0.9, 0.71 + index * 0.02),
            100 + index,
        )
        for index in range(5)
    )
    receipt = replace(
        receipt,
        layout_evidence=replace(
            receipt.layout_evidence,
            likely_item_total_regions=(
                *receipt.layout_evidence.likely_item_total_regions,
                *extra,
            ),
        ),
    )
    selected = select_automatic_end_boundary(receipt, 1000)
    receipt = replace(receipt, merchandise_end_boundary=selected.boundary)
    result = validate_receipt_coverage(receipt, 600, 1000)
    assert result.strong_conflict
    assert result.retry_passes == ("layout", "grouping")


def test_quantity_sum_can_explain_printed_count_and_header_is_not_an_item():
    quantities = (2, 2, 2, 1, 1, 1, 1)
    items = tuple(
        item(index + 1, 0.10 + index * 0.09, quantity)
        for index, quantity in enumerate(quantities)
    )
    header = ReceiptLineFacts(
        source_line_index=0,
        bounding_region=BoundingRegion(0.08, 0.06, 0.92, 0.09),
        raw_printed_text="PRODUCE",
        generic_item_name="",
        brand=None,
        language="en",
        form=FoodForm.UNKNOWN,
        quantity=None,
        total_weight=None,
        unit_weight=None,
        printed_line_total=None,
        classification=ReceiptLineClassification.HEADER,
    )
    receipt = canonical(
        items,
        boundary_top=0.78,
        printed_count=10,
        headers=(header,),
    )
    selected = select_automatic_end_boundary(receipt, 1000)
    receipt = replace(receipt, merchandise_end_boundary=selected.boundary)
    assert validate_receipt_coverage(receipt, 600, 1000).complete
    assert len(receipt.logical_items) == 7


def test_five_segments_need_explicit_manual_boundary_when_auto_boundary_absent():
    receipt = canonical((item(0, 0.2),), boundary_kind=None)
    assert receipt_session_status([receipt] * 4) is ReceiptSessionStatus.CONTINUE_UPLOAD
    assert (
        receipt_session_status([receipt] * 5)
        is ReceiptSessionStatus.MANUAL_REVIEW_REQUIRED
    )
    reviewed = confirm_manual_boundary(
        receipt, last_item_source_index=0, boundary_y=0.30
    )
    assert reviewed.user_confirmed_boundary is not None
    assert reviewed.user_confirmed_boundary.kind is ReceiptBoundaryKind.USER_CONFIRMED
    assert (
        receipt_session_status([receipt] * 4 + [reviewed])
        is ReceiptSessionStatus.AUTO_CONFIRMABLE
    )
    cropped = rebase_receipt_to_boundary_crop(reviewed, 0.30)
    assert cropped.user_confirmed_boundary.bounding_region.y2 == 1.0
    assert cropped.logical_items[0].bounding_region.y2 < 1.0
    assert validate_receipt_coverage(cropped, 600, 300).complete
