"""Loading and failure-detail views for image workflows."""

import flet as ft
from types import SimpleNamespace

from models.photo_analysis import (
    FoodForm,
    ReceiptScanFacts,
    ReceiptScanItem,
    ReceiptScanItemKind,
    WeightFact,
)
from ui.image_processing import ImageFailureDetails, ImageProcessingView
from services.photo_analyzer import ReceiptScanError
from ui.photo_purchase import (
    ACTION_CUSTOM,
    _ReceiptDecision,
    _open_receipt_batch_review_dialog,
    run_product_photo_flow,
    run_receipt_flow,
)


class FakePage:
    def __init__(self):
        self.dialogs = []
        self.update_count = 0
        self.pop_count = 0

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def update(self):
        self.update_count += 1

    def pop_dialog(self):
        self.pop_count += 1


def controls(root):
    yield root
    for attribute in ("title", "content"):
        child = getattr(root, attribute, None)
        if child is not None:
            yield from controls(child)
    for attribute in ("controls", "actions"):
        for child in getattr(root, attribute, None) or ():
            yield from controls(child)


def visible_text(root):
    return " ".join(
        control.value
        for control in controls(root)
        if isinstance(control, ft.Text) and control.value
    )


def test_processing_view_starts_with_modal_spinner():
    page = FakePage()
    view = ImageProcessingView(page, title="Processing product image")
    view.show("Analyzing the product image", "This may take a moment.")
    assert view.active
    assert page.dialogs == [view.dialog]
    assert view.dialog.modal
    assert any(isinstance(control, ft.ProgressRing) for control in controls(view.dialog))
    assert "Analyzing the product image" in visible_text(view.dialog)


def test_processing_view_transitions_to_english_failure_details_and_closes():
    page = FakePage()
    view = ImageProcessingView(page)
    view.show("Preparing the uploaded image")
    view.show_failure(ImageFailureDetails(
        summary="The product image could not be processed.",
        stage="Image analysis",
        reason="The response did not contain valid product facts.",
        suggestions=("Use a clear JPG or PNG image.",),
        diagnostics=(("Final image size", "800 × 1200 px"),),
    ))
    text = visible_text(view.dialog)
    assert "Image processing failed" in text
    assert "Failed stage Image analysis" in text
    assert "What you can try" in text
    assert "Final image size 800 × 1200 px" in text
    assert not any("\u3400" <= character <= "\u9fff" for character in text)
    assert not any(isinstance(control, ft.ProgressRing) for control in controls(view.dialog))
    view.close()
    assert not view.active and page.pop_count == 1


def test_receipt_items_are_reviewed_together_and_only_confirmed_rows_continue():
    rice = ReceiptScanItem(
        source_item_index=0,
        raw_printed_text="RICE 2 LB 4.25",
        generic_item_name="White rice",
        brand=None,
        language="en",
        form=FoodForm.DRY,
        quantity=1,
        total_weight=WeightFact(2, "lb", "2 LB"),
        unit_weight=None,
        printed_line_total=4.25,
        kind=ReceiptScanItemKind.FOOD,
    )
    paper = ReceiptScanItem(
        source_item_index=1,
        raw_printed_text="PAPER TOWELS 8.99",
        generic_item_name="Paper towels",
        brand=None,
        language="en",
        form=FoodForm.UNKNOWN,
        quantity=1,
        total_weight=None,
        unit_weight=None,
        printed_line_total=8.99,
        kind=ReceiptScanItemKind.NON_FOOD,
    )
    decision = _ReceiptDecision(
        item=rice,
        destination=ACTION_CUSTOM,
        display_name="White rice",
        food_id=None,
        amount=2,
        unit="lb",
        grams=907.185,
        grams_source="user_entered",
    )
    captured = {}
    page = FakePage()
    _open_receipt_batch_review_dialog(
        page,
        SimpleNamespace(),
        ReceiptScanFacts("Market", "2026-07-15", "USD", (rice, paper)),
        SimpleNamespace(),
        initial_decisions=(decision,),
        review_reasons={},
        ignored_reasons={ReceiptScanItemKind.NON_FOOD: "detected as non-food"},
        on_confirmed=lambda selected, skipped: captured.update(
            selected=selected, skipped=skipped
        ),
    )

    assert len(page.dialogs) == 1
    dialog = page.dialogs[0]
    assert "Review receipt items" in visible_text(dialog)
    assert "White rice" in visible_text(dialog)
    assert "Paper towels" in visible_text(dialog)
    checks = [control for control in controls(dialog) if isinstance(control, ft.Checkbox)]
    assert len(checks) == 2
    assert checks[0].value and not checks[0].disabled
    assert not checks[1].value and checks[1].disabled
    edits = [
        control for control in controls(dialog)
        if isinstance(control, ft.TextButton) and control.content == "Edit"
    ]
    assert len(edits) == 2

    confirm = next(
        control for control in controls(dialog)
        if isinstance(control, ft.TextButton)
        and control.content == "Import confirmed items"
    )
    confirm.on_click(None)
    assert captured["selected"] == [decision]
    assert captured["skipped"] == ["Paper towels — detected as non-food"]


async def test_product_upload_shows_failure_detail_page(tmp_path, monkeypatch):
    image = tmp_path / "product.jpg"
    image.write_bytes(b"not-a-readable-image")

    class Picker:
        async def pick_files(self, **kwargs):
            return [SimpleNamespace(path=str(image))]

    class Analyzer:
        async def analyze_product(self, content, mime):
            return None

    state = SimpleNamespace(
        photo_imports=[],
        begin_photo_analysis=lambda: 1,
        is_current_photo_analysis=lambda analysis_id: analysis_id == 1,
    )
    page = FakePage()
    monkeypatch.setattr(
        "ui.photo_purchase._guarded_analyzer", lambda current_page, current_state: Analyzer()
    )
    await run_product_photo_flow(page, state, Picker(), lambda: None)
    failure_dialog = next(
        dialog for dialog in reversed(page.dialogs) if isinstance(dialog, ft.AlertDialog)
    )
    assert "Image processing failed" in visible_text(failure_dialog)
    assert "Image analysis" in visible_text(failure_dialog)


async def test_receipt_scan_error_uses_detailed_popup_not_generic_snackbar(
    tmp_path, monkeypatch
):
    image = tmp_path / "receipt.jpg"
    from PIL import Image

    Image.new("RGB", (400, 700), "white").save(image, format="JPEG")

    class Picker:
        async def pick_files(self, **kwargs):
            return [SimpleNamespace(path=str(image))]

    class Analyzer:
        async def scan_receipt(self, content, mime):
            raise ReceiptScanError(
                "invalid_receipt_schema",
                "Response validation",
                "Item 3 had an invalid quantity.",
                suggestions=("Retake the receipt photo.",),
                diagnostics=(("Item", "3"),),
            )

    state = SimpleNamespace(
        purchase_log_error=None,
        photo_import_error=None,
        profile=object(),
        http_client=object(),
        begin_photo_analysis=lambda: 1,
        is_current_photo_analysis=lambda analysis_id: analysis_id == 1,
    )
    page = FakePage()
    monkeypatch.setattr(
        "ui.photo_purchase.get_photo_analyzer",
        lambda profile, http_client: Analyzer(),
    )
    await run_receipt_flow(page, state, Picker(), lambda: None)
    failure_dialog = next(
        dialog for dialog in reversed(page.dialogs) if isinstance(dialog, ft.AlertDialog)
    )
    text = visible_text(failure_dialog)
    assert "Response validation" in text
    assert "Item 3 had an invalid quantity" in text
    assert "Item 3" in text
