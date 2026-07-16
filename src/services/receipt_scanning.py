"""Coordinate-free receipt segment assembly and overlap handling."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Sequence

from models.photo_analysis import ReceiptScanFacts, ReceiptScanItem
from models.quantities import canonical_money, canonical_quantity


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def receipt_item_signature(item: ReceiptScanItem) -> tuple:
    """Observable identity used only for ordered segment-overlap detection."""

    total_weight = (
        (item.total_weight.value, _normalized(item.total_weight.unit))
        if item.total_weight is not None else None
    )
    unit_weight = (
        (item.unit_weight.value, _normalized(item.unit_weight.unit))
        if item.unit_weight is not None else None
    )
    return (
        item.kind.value,
        _normalized(item.raw_printed_text),
        _normalized(item.generic_item_name),
        canonical_quantity(item.quantity or 1),
        total_weight,
        unit_weight,
        canonical_money(item.printed_line_total)
        if item.printed_line_total is not None else None,
    )


def combine_receipt_scans(segments: Sequence[ReceiptScanFacts]) -> ReceiptScanFacts:
    """Combine ordered segments and flag only exact suffix/prefix overlap.

    Repeated lines inside one image remain separate real purchases.  Across two
    adjacent images, only the longest exact ordered overlap is marked as a
    possible duplicate; the UI can review it instead of silently deleting it.
    """

    if not segments:
        raise ValueError("At least one receipt image is required.")
    if len(segments) > 5:
        raise ValueError("A receipt import supports at most five ordered images.")

    combined: list[ReceiptScanItem] = []
    previous_items: tuple[ReceiptScanItem, ...] = ()
    for segment_index, segment in enumerate(segments):
        current = tuple(
            replace(item, segment_index=segment_index)
            for item in segment.items
        )
        overlap = 0
        if previous_items and current:
            maximum = min(len(previous_items), len(current))
            previous_signatures = [receipt_item_signature(item) for item in previous_items]
            current_signatures = [receipt_item_signature(item) for item in current]
            for size in range(maximum, 0, -1):
                if previous_signatures[-size:] == current_signatures[:size]:
                    overlap = size
                    break
        for index, item in enumerate(current):
            if index < overlap:
                item = replace(
                    item,
                    possible_duplicate=True,
                    duplicate_reason="Overlapping receipt image item",
                )
            combined.append(item)
        previous_items = current

    first = segments[0]
    last = segments[-1]
    return ReceiptScanFacts(
        store_name=first.store_name or last.store_name,
        purchase_date=first.purchase_date or last.purchase_date,
        currency=first.currency or last.currency,
        unreadable_item_count=sum(item.unreadable_item_count for item in segments),
        items=tuple(combined),
    )


__all__ = ["combine_receipt_scans", "receipt_item_signature"]
