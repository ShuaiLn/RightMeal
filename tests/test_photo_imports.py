import io
from datetime import datetime

import pytest
from PIL import Image

from models.pantry import CustomPantryItem
from models.photo_analysis import (
    BoundingRegion,
    FoodForm,
    PhotoKind,
    ReceiptFacts,
    ReceiptLineClassification,
    ReceiptLineFacts,
)
from models.photo_import import ImportedImage, PhotoImportRecord
from models.purchase_log import ORIGIN_PRODUCT_PHOTO, PurchaseInput
from services.photo_images import normalize_image
from services.photo_imports import (
    InconsistentPhotoOperation,
    check_duplicate_import,
    commit_photo_import,
    deterministic_custom_pantry_id,
    deterministic_purchase_event_id,
    existing_operation,
    receipt_transaction_fingerprint,
)
from services.profile_store import ProfileStore
from ui.state import AppState


def normalized_image():
    output = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(output, format="PNG")
    return normalize_image(output.getvalue())


def receipt():
    line = ReceiptLineFacts(
        source_line_index=0,
        bounding_region=BoundingRegion(0.1, 0.2, 0.9, 0.3),
        raw_printed_text="MILK 3.25",
        generic_item_name="milk",
        brand=None,
        language="en",
        form=FoodForm.FRESH,
        quantity=1,
        total_weight=None,
        unit_weight=None,
        printed_line_total=3.25,
        classification=ReceiptLineClassification.MERCHANDISE,
    )
    return ReceiptFacts(
        store_name="Market",
        purchase_date="2026-07-14",
        currency="USD",
        estimated_visible_merchandise_line_count=1,
        merchandise_area=BoundingRegion(0.05, 0.1, 0.95, 0.4),
        bottom_visible=True,
        lines=(line,),
    )


def state(tmp_path):
    return AppState(store=ProfileStore(tmp_path))


def test_deterministic_child_ids_are_stable_and_distinct():
    operation = "op-1"
    assert deterministic_purchase_event_id(operation, 0, 0) == deterministic_purchase_event_id(
        operation, 0, 0
    )
    assert deterministic_purchase_event_id(operation, 0, 0) != deterministic_purchase_event_id(
        operation, 0, 1
    )
    assert deterministic_custom_pantry_id(operation, 0, 0).startswith("custom:")


def test_receipt_fingerprint_requires_reliable_fields_and_is_hashed():
    fingerprint = receipt_transaction_fingerprint(receipt())
    assert fingerprint is not None and len(fingerprint) == 64
    no_store = receipt()
    no_store = ReceiptFacts(**{**no_store.__dict__, "store_name": None})
    assert receipt_transaction_fingerprint(no_store) is None


def test_duplicate_receipt_blocks_even_when_purchase_history_changes():
    image = normalized_image()
    record = PhotoImportRecord(
        operation_id="op",
        photo_kind=PhotoKind.RECEIPT,
        imported_at="2026-07-14T10:00:00",
        images=(ImportedImage(image.sha256, "imported_images/op-0.png", 0),),
        transaction_fingerprint=receipt_transaction_fingerprint(receipt()),
        purchase_event_ids=("event-that-may-be-voided",),
        custom_pantry_ids=(),
    )
    result = check_duplicate_import(
        PhotoKind.RECEIPT, [image.sha256], None, [record]
    )
    assert result.blocked
    assert "July 14, 2026" in result.message


def test_duplicate_product_requires_second_confirmation_but_is_not_blocked():
    image = normalized_image()
    record = PhotoImportRecord(
        operation_id="op",
        photo_kind=PhotoKind.PRODUCT,
        imported_at="2026-07-14T10:00:00",
        images=(ImportedImage(image.sha256, "imported_images/op-0.png", 0),),
        transaction_fingerprint=None,
        purchase_event_ids=(),
        custom_pantry_ids=("custom:x",),
    )
    result = check_duplicate_import(PhotoKind.PRODUCT, [image.sha256], None, [record])
    assert not result.blocked
    assert result.requires_confirmation
    assert result.message == "This image was imported on July 14, 2026. Continue anyway?"


def test_commit_and_retry_do_not_add_grams_twice(tmp_path):
    app = state(tmp_path)
    operation = "operation-1"
    event_id = deterministic_purchase_event_id(operation, 0, 0)
    purchase = PurchaseInput(
        event_id=event_id,
        food_id="rice_white",
        raw_name="Rice",
        grams=500,
        origin=ORIGIN_PRODUCT_PHOTO,
        source_line_index=0,
        segment_index=0,
    )
    image = normalized_image()
    first = commit_photo_import(
        app,
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        purchase_inputs=[purchase],
        now=datetime(2026, 7, 14, 12, 0, 0),
    )
    assert not first.replayed
    assert app.pantry.items["rice_white"] == 500
    assert len(app.purchase_log) == 1
    assert (tmp_path / first.record.images[0].local_path).is_file()

    second = commit_photo_import(
        app,
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        purchase_inputs=[purchase],
    )
    assert second.replayed
    assert app.pantry.items["rice_white"] == 500
    assert len(app.purchase_log) == 1


def test_custom_unknown_grams_is_inert_and_uses_product_image(tmp_path):
    app = state(tmp_path)
    operation = "custom-operation"
    custom = CustomPantryItem(
        id=deterministic_custom_pantry_id(operation, 0, 0),
        original_name="Unmapped food",
        display_name="Unmapped food",
        amount=1,
        unit="item",
        grams_estimate=0,
    )
    result = commit_photo_import(
        app,
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[normalized_image()],
        purchase_inputs=[],
        custom_items=[custom],
    )
    assert not app.pantry.items
    assert app.pantry.custom_items[0].image_source == "uploaded_product"
    assert app.pantry.custom_items[0].image_path == result.record.images[0].local_path


def test_partial_operation_is_blocked_for_review():
    operation = "partial"
    ledger = [PhotoImportRecord(
        operation_id=operation,
        photo_kind=PhotoKind.PRODUCT,
        imported_at="2026-07-14T10:00:00",
        images=(),
        transaction_fingerprint=None,
        purchase_event_ids=(deterministic_purchase_event_id(operation, 0, 0),),
        custom_pantry_ids=(),
    )]
    with pytest.raises(InconsistentPhotoOperation):
        existing_operation(operation, ledger, [], [])


def test_ledger_rejects_raw_model_data_and_unsafe_paths():
    record = {
        "operation_id": "op",
        "photo_kind": "product",
        "imported_at": "2026-07-14T10:00:00",
        "images": [{
            "sha256": "a" * 64,
            "local_path": "imported_images/op.png",
            "segment_index": 0,
        }],
        "transaction_fingerprint": None,
        "purchase_event_ids": [],
        "custom_pantry_ids": [],
        "raw_ai_response": {"must": "never persist"},
    }
    with pytest.raises(ValueError):
        PhotoImportRecord.from_dict(record)
    del record["raw_ai_response"]
    record["images"][0]["local_path"] = "../receipt.png"
    with pytest.raises(ValueError):
        PhotoImportRecord.from_dict(record)


def test_persist_failure_restores_memory_and_removes_image(tmp_path, monkeypatch):
    app = state(tmp_path)
    operation = "failing-operation"
    event_id = deterministic_purchase_event_id(operation, 0, 0)
    purchase = PurchaseInput(
        event_id=event_id,
        food_id="rice_white",
        grams=100,
        origin=ORIGIN_PRODUCT_PHOTO,
        source_line_index=0,
        segment_index=0,
    )
    monkeypatch.setattr(app, "persist", lambda **kwargs: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        commit_photo_import(
            app,
            operation_id=operation,
            kind=PhotoKind.PRODUCT,
            images=[normalized_image()],
            purchase_inputs=[purchase],
        )
    assert not app.pantry.items
    assert not app.purchase_log
    assert not app.photo_imports
    assert not list((tmp_path / "imported_images").glob("failing-operation*"))
