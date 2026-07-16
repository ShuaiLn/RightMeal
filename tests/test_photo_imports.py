import io
from dataclasses import replace
from datetime import datetime
from pathlib import Path

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
from models.purchase_log import ORIGIN_PRODUCT_PHOTO, ORIGIN_RECEIPT, PurchaseInput
from services.photo_images import normalize_image
from services.photo_imports import (
    DuplicateAcknowledgement,
    InconsistentPhotoOperation,
    PhotoImportCommand,
    StalePhotoImportContext,
    check_duplicate_import,
    commit_photo_import,
    deterministic_custom_pantry_id,
    deterministic_purchase_event_id,
    dialog_context_for_state,
    existing_operation,
    receipt_transaction_fingerprint,
)
from services.profile_store import ProfileStore
from services.tx import TransactionRecoveryRequiredError
from ui.state import AppState


def normalized_image(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (100, 100), color).save(output, format="PNG")
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


def test_receipt_fingerprint_canonicalizes_equivalent_money_values():
    base = receipt()
    fingerprints = {
        receipt_transaction_fingerprint(replace(
            base,
            lines=(replace(base.lines[0], printed_line_total=value),),
        ))
        for value in (1, 1.0, 1.00)
    }
    assert len(fingerprints) == 1


def test_receipt_fingerprint_ignores_auxiliary_visible_count_estimate():
    original = receipt()
    assert receipt_transaction_fingerprint(original) == receipt_transaction_fingerprint(
        replace(original, estimated_visible_merchandise_line_count=99)
    )


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


def test_receipt_duplicate_tracks_remaining_pantry_items_and_can_be_acknowledged(tmp_path):
    app = state(tmp_path)
    image = normalized_image()
    repeat_image = normalized_image("black")
    first_operation = "first-receipt"
    fingerprint = receipt_transaction_fingerprint(receipt())
    first_purchase = PurchaseInput(
        event_id=deterministic_purchase_event_id(first_operation, 0, 0),
        food_id="rice_white",
        grams=100,
        origin=ORIGIN_RECEIPT,
        source_line_index=0,
        segment_index=0,
    )
    first = commit_photo_import(
        app,
        operation_id=first_operation,
        kind=PhotoKind.RECEIPT,
        images=[image],
        purchase_inputs=[first_purchase],
        purchase_units={first_purchase.event_id: "g"},
        transaction_fingerprint=fingerprint,
    )

    duplicate = check_duplicate_import(
        PhotoKind.RECEIPT,
        [repeat_image.sha256],
        fingerprint,
        app.photo_imports,
        purchases=app.purchase_log,
        pantry=app.pantry,
    )
    assert not duplicate.blocked
    assert duplicate.requires_confirmation
    assert duplicate.matched_image_hash is None
    assert "some of its Pantry items still remain" in (duplicate.message or "")

    second_operation = "second-receipt"
    second_purchase = replace(
        first_purchase,
        event_id=deterministic_purchase_event_id(second_operation, 0, 0),
    )
    context = dialog_context_for_state(
        app, operation_id=second_operation, analysis_id=0
    )
    command = PhotoImportCommand.create(
        operation_id=second_operation,
        kind=PhotoKind.RECEIPT,
        images=[repeat_image],
        plan_id=None,
        context=context,
        purchase_inputs=[second_purchase],
        purchase_units={second_purchase.event_id: "g"},
        transaction_fingerprint=fingerprint,
        duplicate_acknowledgement=DuplicateAcknowledgement(
            operation_id=second_operation,
            previous_operation_id=first.record.operation_id,
            image_hash=None,
            ledger_revision=app.photo_import_revision,
        ),
    )
    commit_photo_import(app, command=command, images=[repeat_image])
    assert app.pantry.items["rice_white"] == 200

    app.pantry.set_grams("rice_white", 0)
    cleared = check_duplicate_import(
        PhotoKind.RECEIPT,
        [repeat_image.sha256],
        fingerprint,
        app.photo_imports,
        purchases=app.purchase_log,
        pantry=app.pantry,
    )
    assert not cleared.blocked
    assert not cleared.requires_confirmation


def test_receipt_duplicate_ignores_linked_custom_item_after_food_is_deleted(tmp_path):
    app = state(tmp_path)
    image = normalized_image()
    custom = CustomPantryItem(
        id="custom:receipt-item",
        original_name="Unknown rice",
        display_name="Unknown rice",
        amount=1,
        unit="bag",
        grams_estimate=100,
    )
    app.pantry.add_custom_item(custom)
    record = PhotoImportRecord(
        operation_id="custom-receipt",
        photo_kind=PhotoKind.RECEIPT,
        imported_at="2026-07-14T10:00:00",
        images=(ImportedImage(image.sha256, "imported_images/custom-0.png", 0),),
        transaction_fingerprint=receipt_transaction_fingerprint(receipt()),
        purchase_event_ids=(),
        custom_pantry_ids=(custom.id,),
    )
    assert check_duplicate_import(
        PhotoKind.RECEIPT,
        [image.sha256],
        record.transaction_fingerprint,
        [record],
        pantry=app.pantry,
    ).requires_confirmation

    assert app.pantry.link_custom_item(custom.id, "rice_white")
    app.pantry.set_grams("rice_white", 0)
    assert not check_duplicate_import(
        PhotoKind.RECEIPT,
        [image.sha256],
        record.transaction_fingerprint,
        [record],
        pantry=app.pantry,
    ).requires_confirmation


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
        purchase_units={event_id: "g"},
        now=datetime(2026, 7, 14, 12, 0, 0),
    )
    assert not first.replayed
    assert app.pantry.items["rice_white"] == 500
    assert len(app.purchase_log) == 1
    assert (tmp_path / first.record.images[0].local_path).is_file()
    assert (
        app.plan_revision,
        app.pantry_revision,
        app.purchase_revision,
        app.photo_import_revision,
    ) == (0, 1, 1, 1)

    second = commit_photo_import(
        app,
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        purchase_inputs=[purchase],
        purchase_units={event_id: "g"},
    )
    assert second.replayed
    assert app.pantry.items["rice_white"] == 500
    assert len(app.purchase_log) == 1
    assert (
        app.plan_revision,
        app.pantry_revision,
        app.purchase_revision,
        app.photo_import_revision,
    ) == (0, 1, 1, 1)


def test_same_operation_with_changed_confirmed_payload_fails_closed(tmp_path):
    app = state(tmp_path)
    operation = "same-operation"
    image = normalized_image()
    event_id = deterministic_purchase_event_id(operation, 0, 0)

    def purchase(grams):
        return PurchaseInput(
            event_id=event_id,
            food_id="rice_white",
            grams=grams,
            origin=ORIGIN_PRODUCT_PHOTO,
            source_line_index=0,
            segment_index=0,
        )

    commit_photo_import(
        app,
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        purchase_inputs=[purchase(500)],
        purchase_units={event_id: "g"},
    )
    with pytest.raises(InconsistentPhotoOperation, match="different"):
        commit_photo_import(
            app,
            operation_id=operation,
            kind=PhotoKind.PRODUCT,
            images=[image],
            purchase_inputs=[purchase(501)],
            purchase_units={event_id: "g"},
        )
    assert app.pantry.items["rice_white"] == 500
    assert len(app.purchase_log) == 1


def test_command_fingerprint_excludes_raw_ai_display_payload(tmp_path):
    app = state(tmp_path)
    image = normalized_image()
    operation = "fingerprint"
    context = dialog_context_for_state(app, operation_id=operation, analysis_id=0)
    base = PurchaseInput(
        event_id=deterministic_purchase_event_id(operation, 0, 0),
        food_id="rice_white",
        raw_name="AI RAW NAME",
        brand="AI BRAND",
        grams=500,
        quantity=1,
        line_total=3.25,
        source_line_index=0,
        segment_index=0,
    )
    first = PhotoImportCommand.create(
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        plan_id=None,
        context=context,
        purchase_inputs=[base],
        purchase_units={base.event_id: "bag"},
    )
    changed_raw = replace(base, raw_name="DIFFERENT AI TEXT", brand="OTHER", line_total=9.99)
    second = PhotoImportCommand.create(
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        plan_id=None,
        context=context,
        purchase_inputs=[changed_raw],
        purchase_units={base.event_id: "bag"},
    )
    assert first.commit_fingerprint == second.commit_fingerprint
    changed_quantity = replace(base, quantity=2, grams=1000)
    third = PhotoImportCommand.create(
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        plan_id=None,
        context=context,
        purchase_inputs=[changed_quantity],
        purchase_units={base.event_id: "bag"},
    )
    assert first.commit_fingerprint != third.commit_fingerprint


def test_command_never_invents_a_unit_for_service_callers(tmp_path):
    app = state(tmp_path)
    image = normalized_image()
    operation = "explicit-unit"
    context = dialog_context_for_state(app, operation_id=operation, analysis_id=0)
    purchase = PurchaseInput(
        event_id=deterministic_purchase_event_id(operation, 0, 0),
        food_id="rice_white",
        grams=100,
        source_line_index=0,
        segment_index=0,
    )

    with pytest.raises(ValueError, match="explicit unit"):
        PhotoImportCommand.create(
            operation_id=operation,
            kind=PhotoKind.PRODUCT,
            images=[image],
            plan_id=None,
            context=context,
            purchase_inputs=[purchase],
        )

    assert not app.pantry.items
    assert not app.purchase_log
    assert not app.photo_imports


def test_expected_revision_mismatch_writes_nothing(tmp_path):
    app = state(tmp_path)
    image = normalized_image()
    operation = "stale"
    context = dialog_context_for_state(app, operation_id=operation, analysis_id=0)
    purchase = PurchaseInput(
        event_id=deterministic_purchase_event_id(operation, 0, 0),
        food_id="rice_white",
        grams=100,
        source_line_index=0,
        segment_index=0,
    )
    command = PhotoImportCommand.create(
        operation_id=operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        plan_id=None,
        context=context,
        purchase_inputs=[purchase],
        purchase_units={purchase.event_id: "g"},
    )
    app.persist(pantry=app.pantry)  # advances Pantry revision after command snapshot
    with pytest.raises(StalePhotoImportContext, match="Pantry"):
        commit_photo_import(app, command=command, images=[image])
    assert not app.purchase_log
    assert not app.photo_imports
    assert not list((tmp_path / "imported_images").glob("stale*"))


def test_product_duplicate_tracks_active_output_and_acknowledgement(tmp_path):
    app = state(tmp_path)
    image = normalized_image()
    first_operation = "first-active"
    first_purchase = PurchaseInput(
        event_id=deterministic_purchase_event_id(first_operation, 0, 0),
        food_id="rice_white",
        grams=100,
        source_line_index=0,
        segment_index=0,
    )
    first = commit_photo_import(
        app,
        operation_id=first_operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        purchase_inputs=[first_purchase],
        purchase_units={first_purchase.event_id: "g"},
    )
    duplicate = check_duplicate_import(
        PhotoKind.PRODUCT,
        [image.sha256],
        None,
        app.photo_imports,
        purchases=app.purchase_log,
        pantry=app.pantry,
    )
    assert duplicate.requires_confirmation

    second_operation = "second-active"
    context = dialog_context_for_state(app, operation_id=second_operation, analysis_id=0)
    second_purchase = replace(
        first_purchase,
        event_id=deterministic_purchase_event_id(second_operation, 0, 0),
    )
    command = PhotoImportCommand.create(
        operation_id=second_operation,
        kind=PhotoKind.PRODUCT,
        images=[image],
        plan_id=None,
        context=context,
        purchase_inputs=[second_purchase],
        purchase_units={second_purchase.event_id: "g"},
        duplicate_acknowledgement=DuplicateAcknowledgement(
            operation_id=second_operation,
            previous_operation_id=first.record.operation_id,
            image_hash=image.sha256,
            ledger_revision=app.photo_import_revision,
        ),
    )
    commit_photo_import(app, command=command, images=[image])
    assert len(app.purchase_log) == 2

    app.pantry.set_grams("rice_white", 0)
    assert not check_duplicate_import(
        PhotoKind.PRODUCT,
        [image.sha256],
        None,
        app.photo_imports,
        purchases=app.purchase_log,
        pantry=app.pantry,
    ).requires_confirmation


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
            purchase_units={event_id: "g"},
        )
    assert not app.pantry.items
    assert not app.purchase_log
    assert not app.photo_imports
    assert not list((tmp_path / "imported_images").glob("failing-operation*"))


def test_journal_cleanup_failure_keeps_matching_memory_and_freezes(tmp_path, monkeypatch):
    app = state(tmp_path)
    operation = "recovery-operation"
    purchase = PurchaseInput(
        event_id=deterministic_purchase_event_id(operation, 0, 0),
        food_id="rice_white",
        grams=100,
        origin=ORIGIN_PRODUCT_PHOTO,
        source_line_index=0,
        segment_index=0,
    )
    journal = app.tx.journal_path
    real_unlink = Path.unlink

    def failing_unlink(path, *args, **kwargs):
        if path == journal:
            raise OSError("journal locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(TransactionRecoveryRequiredError):
        commit_photo_import(
            app,
            operation_id=operation,
            kind=PhotoKind.PRODUCT,
            images=[normalized_image()],
            purchase_inputs=[purchase],
            purchase_units={purchase.event_id: "g"},
        )
    assert app.pantry.items["rice_white"] == 100
    assert len(app.purchase_log) == len(app.photo_imports) == 1
    assert app.photo_import_error is not None
    assert app.tx.writes_frozen and journal.exists()
    assert (
        app.pantry_revision,
        app.purchase_revision,
        app.photo_import_revision,
    ) == (0, 0, 0)
    assert (tmp_path / app.photo_imports[0].images[0].local_path).is_file()
