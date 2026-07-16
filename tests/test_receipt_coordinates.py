"""Declared wire-coordinate normalization and staged validation."""

from dataclasses import replace

import pytest

from models.photo_analysis import (
    BoundingRegion,
    CoordinateOrder,
    CoordinateProtocolError,
    CoordinateSpace,
    FoodForm,
    ReceiptFacts,
    ReceiptLineClassification,
    ReceiptLineFacts,
    photo_analysis_from_dict,
)
from services.receipt_validation import (
    combine_receipt_segments,
    receipt_session_confirmable,
    validate_receipt_coverage,
)


def payload(space="normalized", order="left_top_right_bottom"):
    if space == "normalized":
        area, line = [0.1, 0.1, 0.9, 0.9], [0.2, 0.2, 0.8, 0.3]
    elif space == "percent":
        area, line = [10, 10, 90, 90], [20, 20, 80, 30]
    else:
        area, line = [20, 10, 180, 90], [40, 20, 160, 30]
    if order == "top_left_bottom_right":
        area = [area[1], area[0], area[3], area[2]]
        line = [line[1], line[0], line[3], line[2]]
    return {
        "kind": "receipt",
        "product": None,
        "receipt": {
            "store_name": None,
            "purchase_date": None,
            "currency": None,
            "estimated_visible_merchandise_line_count": 1,
            "merchandise_area": area,
            "coordinate_space": space,
            "coordinate_order": order,
            "bottom_visible": False,
            "lines": [{
                "source_line_index": 0,
                "bounding_region": line,
                "raw_printed_text": "ITEM",
                "generic_item_name": "item",
                "brand": None,
                "language": None,
                "form": "unknown",
                "quantity": 1,
                "total_weight": None,
                "unit_weight": None,
                "printed_line_total": None,
                "classification": "merchandise",
            }],
        },
        "observed_summary": "receipt",
    }


@pytest.mark.parametrize("space", ["normalized", "percent", "pixels"])
@pytest.mark.parametrize(
    "order", ["left_top_right_bottom", "top_left_bottom_right"]
)
def test_declared_spaces_and_orders_create_one_canonical_ltbr_model(space, order):
    parsed = photo_analysis_from_dict(
        payload(space, order), image_width=200, image_height=100
    )
    receipt = parsed.receipt
    assert receipt is not None
    assert receipt.merchandise_area.as_tuple() == pytest.approx((0.1, 0.1, 0.9, 0.9))
    assert receipt.lines[0].bounding_region.as_tuple() == pytest.approx(
        (0.2, 0.2, 0.8, 0.3)
    )
    assert receipt.coordinate_space is CoordinateSpace(space)
    assert receipt.coordinate_order is CoordinateOrder(order)


def test_missing_or_conflicting_declaration_fails_without_guessing_or_clamping():
    missing = payload()
    del missing["receipt"]["coordinate_space"]
    with pytest.raises(CoordinateProtocolError, match="declarations are required"):
        photo_analysis_from_dict(missing, image_width=200, image_height=100)

    conflicting = payload()
    conflicting["receipt"]["merchandise_area"] = [20, 30, 80, 40]
    with pytest.raises(CoordinateProtocolError, match="conflicts"):
        photo_analysis_from_dict(conflicting, image_width=200, image_height=100)


def domain_line(index, region):
    return ReceiptLineFacts(
        source_line_index=index,
        bounding_region=region,
        raw_printed_text=f"ITEM {index}",
        generic_item_name="item",
        brand=None,
        language=None,
        form=FoodForm.UNKNOWN,
        quantity=1,
        total_weight=None,
        unit_weight=None,
        printed_line_total=None,
        classification=ReceiptLineClassification.MERCHANDISE,
    )


def domain_receipt(lines, *, bottom=True, estimate=None):
    return ReceiptFacts(
        store_name=None,
        purchase_date=None,
        currency=None,
        estimated_visible_merchandise_line_count=(
            len(lines) if estimate is None else estimate
        ),
        merchandise_area=BoundingRegion(0.1, 0.1, 0.9, 0.8),
        bottom_visible=bottom,
        lines=tuple(lines),
    )


def test_staged_validation_allows_tolerance_and_varied_line_shapes():
    value = domain_receipt([
        domain_line(0, BoundingRegion(0.0995, 0.15, 0.3, 0.17)),
        domain_line(1, BoundingRegion(0.55, 0.1495, 0.88, 0.32)),
        domain_line(2, BoundingRegion(0.2, 0.5, 0.21, 0.79)),
    ])
    assert validate_receipt_coverage(value, 1000, 1200).complete


def test_coordinate_failure_short_circuits_derived_coverage_errors():
    value = domain_receipt(
        [domain_line(0, BoundingRegion(-0.1, 0.2, 0.2, 0.3))],
        estimate=20,
    )
    result = validate_receipt_coverage(value, 100, 100)
    assert result.failure_stage == "coordinates"
    assert result.reasons == ("Line 0 has invalid coordinates.",)


def test_two_to_five_segment_indices_are_frozen_and_last_bottom_is_required():
    segments = [
        domain_receipt(
            [domain_line(0, BoundingRegion(0.2, 0.2, 0.8, 0.3))],
            bottom=index == 4,
        )
        for index in range(5)
    ]
    combined = combine_receipt_segments(segments)
    assert [line.segment_index for line in combined.lines] == list(range(5))
    assert receipt_session_confirmable(segments)
    assert not receipt_session_confirmable([
        replace(segments[0], bottom_visible=False),
        replace(segments[1], bottom_visible=False),
    ])
    with pytest.raises(ValueError, match="at most five"):
        combine_receipt_segments([*segments, segments[-1]])


def test_same_text_in_non_adjacent_segments_is_not_deduplicated():
    first = domain_receipt([domain_line(0, BoundingRegion(0.2, 0.2, 0.8, 0.3))])
    middle = replace(
        first,
        lines=(replace(first.lines[0], raw_printed_text="OTHER"),),
        bottom_visible=False,
    )
    last = replace(first, bottom_visible=True)
    combined = combine_receipt_segments([first, middle, last])
    assert not combined.lines[2].possible_duplicate
