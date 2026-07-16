"""Coordinate-free receipt extraction, overlap, and auto-routing rules."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from data.loader import load_catalog
from models.photo_analysis import (
    FoodForm,
    ReceiptScanFacts,
    ReceiptScanItem,
    ReceiptScanItemKind,
    WeightFact,
)
from services.pantry_matcher import MatchResult
from services.photo_analyzer import PhotoAnalyzer, ReceiptScanError
from services.photo_resolution import confirmed_line_total
from services.receipt_scanning import combine_receipt_scans
from ui.photo_purchase import ACTION_APPLY, ACTION_CUSTOM, _automatic_receipt_decision


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (700, 1000), "white").save(output, format="JPEG")
    return output.getvalue()


def item(
    index: int,
    name: str = "white rice",
    *,
    kind: ReceiptScanItemKind = ReceiptScanItemKind.FOOD,
    total: float = 4.25,
) -> ReceiptScanItem:
    return ReceiptScanItem(
        source_item_index=index,
        raw_printed_text=f"{name} 2 LB {total:.2f}",
        generic_item_name=name,
        brand=None,
        language="en",
        form=FoodForm.DRY,
        quantity=1,
        total_weight=WeightFact(2, "lb", "2 LB"),
        unit_weight=None,
        printed_line_total=total,
        kind=kind,
    )


def payload(*, unreadable: int = 0) -> dict:
    return {
        "kind": "receipt",
        "receipt": {
            "store_name": "Example Market",
            "purchase_date": "2026-07-15",
            "currency": "USD",
            "unreadable_item_count": unreadable,
            "items": [{
                "source_item_index": 0,
                "raw_printed_text": "RICE 2 LB 4.25",
                "generic_item_name": "white rice",
                "brand": None,
                "language": "en",
                "form": "dry",
                "quantity": 1,
                "total_weight": {"value": 2, "unit": "lb", "raw_text": "2 LB"},
                "unit_weight": None,
                "printed_line_total": 4.25,
                "kind": "food",
            }],
        },
        "observed_summary": "One readable grocery item.",
    }


def client(result: dict, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "failed"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(result)}}]},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_coordinate_free_scan_uses_one_request_and_no_geometry():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload())}}]},
        )

    result = await PhotoAnalyzer(
        "sk-test", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ).scan_receipt(image_bytes())
    assert result.receipt.items[0].generic_item_name == "white rice"
    assert len(requests) == 1
    request_text = json.dumps(requests[0]).casefold()
    assert requests[0]["response_format"]["json_schema"]["name"] == (
        "coordinate_free_receipt_scan"
    )
    assert "bounding_region" not in request_text
    assert "coordinate_space" not in request_text
    assert "merchandise_area" not in request_text


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "API key"), (429, "rate-limited"), (503, "temporary server error")],
)
async def test_scan_maps_http_failures_to_specific_popup_reasons(status, expected):
    analyzer = PhotoAnalyzer("sk-test", client({}, status=status))
    with pytest.raises(ReceiptScanError, match=expected) as error:
        await analyzer.scan_receipt(image_bytes())
    assert error.value.stage == "OpenAI request"


async def test_scan_blocks_when_a_purchased_line_is_unreadable():
    analyzer = PhotoAnalyzer("sk-test", client(payload(unreadable=2)))
    with pytest.raises(ReceiptScanError, match="2 purchased line") as error:
        await analyzer.scan_receipt(image_bytes())
    assert error.value.stage == "Receipt item extraction"


def test_ordered_segment_overlap_is_flagged_without_deleting_real_items():
    first = ReceiptScanFacts("Market", "2026-07-15", "USD", (item(0, "rice"), item(1, "milk")))
    second = ReceiptScanFacts("Market", "2026-07-15", "USD", (item(0, "milk"), item(1, "eggs")))
    combined = combine_receipt_scans((first, second))
    assert len(combined.items) == 4
    assert not combined.items[1].possible_duplicate
    assert combined.items[2].possible_duplicate
    assert combined.items[3].generic_item_name == "eggs"


class Matcher:
    def __init__(self, food_id: str | None):
        self.food_id = food_id

    def match(self, facts, *, plan_food_ids=()):
        return MatchResult("rice", (), self.food_id, True)


def test_high_confidence_plan_food_routes_to_pantry_and_other_food_to_custom():
    rice = next(food for food in load_catalog() if food.id == "rice_white")
    live_plan = SimpleNamespace(
        end_date=date.today() + timedelta(days=1),
        basket=(SimpleNamespace(food_id=rice.id),),
    )
    state = SimpleNamespace(
        saved_plan=live_plan,
        foods_by_id={rice.id: rice},
        purchase_log=[],
    )
    pantry_decision, reason = _automatic_receipt_decision(
        state, item(0), Matcher(rice.id)
    )
    assert reason is None
    assert pantry_decision.destination == ACTION_APPLY
    assert pantry_decision.grams > 900

    state.saved_plan = SimpleNamespace(
        end_date=date.today() + timedelta(days=1),
        basket=(),
    )
    custom_decision, reason = _automatic_receipt_decision(
        state, item(0), Matcher(rice.id)
    )
    assert reason is None
    assert custom_decision.destination == ACTION_CUSTOM


def test_nonfood_and_unknown_items_never_auto_add():
    state = SimpleNamespace(saved_plan=None, foods_by_id={}, purchase_log=[])
    decision, reason = _automatic_receipt_decision(
        state,
        item(0, "paper towels", kind=ReceiptScanItemKind.NON_FOOD),
        Matcher(None),
    )
    assert decision is None
    assert "not confidently classified as food" in reason


def test_coordinate_free_items_reuse_existing_receipt_price_safety_rule():
    offer = item(0)
    offer = ReceiptScanItem(
        **{
            **offer.__dict__,
            "raw_printed_text": "2 FOR $5",
            "quantity": 1,
            "printed_line_total": 5.0,
        }
    )
    assert confirmed_line_total(offer) is None
