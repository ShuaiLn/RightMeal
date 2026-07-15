"""Pure duplicate, fingerprint, and idempotency rules for photo imports."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Sequence

from models.pantry import CustomPantryItem
from models.photo_analysis import PhotoKind, ReceiptFacts, ReceiptLineClassification
from models.photo_import import PhotoImportRecord
from models.plan import RIGHTMEAL_NS
from models.purchase_log import PurchaseRecord
from models.purchase_log import PurchaseInput
from services.pantry_flow import record_purchase_events
from services.photo_images import NormalizedImage
from services.photo_import_store import IMPORTED_IMAGES_DIRNAME


@dataclass(frozen=True)
class DuplicateImportCheck:
    blocked: bool
    requires_confirmation: bool
    previous_import_at: str | None
    message: str | None


class InconsistentPhotoOperation(RuntimeError):
    pass


@dataclass(frozen=True)
class PhotoDialogContext:
    operation_id: str
    analysis_id: int
    plan_id: str | None
    catalog_signature: str


@dataclass(frozen=True)
class DialogContextValidation:
    valid: bool
    rerun_matcher: bool
    message: str | None


@dataclass(frozen=True)
class PhotoImportCommitResult:
    record: PhotoImportRecord
    replayed: bool


def new_operation_id() -> str:
    return str(uuid.uuid4())


def validate_dialog_context(
    context: PhotoDialogContext,
    *,
    current_analysis_id: int,
    current_plan_id: str | None,
    current_catalog_signature: str,
) -> DialogContextValidation:
    if context.analysis_id != current_analysis_id:
        return DialogContextValidation(
            False, False, "A newer photo analysis replaced this dialog."
        )
    if context.catalog_signature != current_catalog_signature:
        return DialogContextValidation(
            False, True, "The catalog changed; candidates were refreshed. Confirm again."
        )
    if context.plan_id != current_plan_id:
        return DialogContextValidation(
            False, False, "The current Plan changed; destinations were refreshed. Confirm again."
        )
    return DialogContextValidation(True, False, None)


def deterministic_purchase_event_id(
    operation_id: str,
    segment_index: int,
    source_line_index: int,
) -> str:
    return str(uuid.uuid5(
        RIGHTMEAL_NS,
        f"photo-import|{operation_id}|purchase|{segment_index}|{source_line_index}",
    ))


def deterministic_custom_pantry_id(
    operation_id: str,
    segment_index: int,
    source_line_index: int,
) -> str:
    value = uuid.uuid5(
        RIGHTMEAL_NS,
        f"photo-import|{operation_id}|custom|{segment_index}|{source_line_index}",
    )
    return f"custom:{value}"


def receipt_transaction_fingerprint(receipt: ReceiptFacts) -> str | None:
    """Hash a reliable transaction summary; never store its raw components."""

    totals = [
        round(line.printed_line_total, 2)
        for line in receipt.lines
        if line.classification is ReceiptLineClassification.MERCHANDISE
        and line.printed_line_total is not None
        and line.printed_line_total > 0
    ]
    if not receipt.store_name or not receipt.purchase_date or not totals:
        return None
    payload = {
        "store": " ".join(receipt.store_name.casefold().split()),
        "date": receipt.purchase_date,
        "currency": (receipt.currency or "").upper(),
        "line_totals": sorted(totals),
        "line_count": receipt.estimated_visible_merchandise_line_count,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def check_duplicate_import(
    kind: PhotoKind,
    image_hashes: Sequence[str],
    transaction_fingerprint: str | None,
    ledger: Sequence[PhotoImportRecord],
) -> DuplicateImportCheck:
    hashes = set(image_hashes)
    for record in reversed(ledger):
        hash_match = bool(hashes.intersection(image.sha256 for image in record.images))
        fingerprint_match = bool(
            transaction_fingerprint
            and record.transaction_fingerprint == transaction_fingerprint
        )
        if not hash_match and not fingerprint_match:
            continue
        display_date = _display_import_date(record.imported_at)
        if kind is PhotoKind.RECEIPT:
            return DuplicateImportCheck(
                blocked=True,
                requires_confirmation=False,
                previous_import_at=record.imported_at,
                message=f"This receipt was already imported on {display_date}.",
            )
        return DuplicateImportCheck(
            blocked=False,
            requires_confirmation=True,
            previous_import_at=record.imported_at,
            message=f"This image was imported on {display_date}. Continue anyway?",
        )
    return DuplicateImportCheck(False, False, None, None)


def _display_import_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return f"{parsed:%B} {parsed.day}, {parsed.year}"


def existing_operation(
    operation_id: str,
    ledger: Sequence[PhotoImportRecord],
    purchases: Sequence[PurchaseRecord],
    custom_items: Sequence[CustomPantryItem],
) -> PhotoImportRecord | None:
    """Return a fully committed retry, or block a partially present operation."""

    record = next((item for item in ledger if item.operation_id == operation_id), None)
    purchase_ids = {item.event_id for item in purchases}
    custom_ids = {item.id for item in custom_items}
    if record is not None:
        purchases_complete = set(record.purchase_event_ids).issubset(purchase_ids)
        customs_complete = set(record.custom_pantry_ids).issubset(custom_ids)
        if purchases_complete and customs_complete:
            return record
        raise InconsistentPhotoOperation(
            "This photo operation is partially present and must be reviewed."
        )

    # Deterministic child rows without their ledger commit are also partial.
    derived_purchase = any(
        _belongs_to_operation(item.event_id, operation_id, "purchase") for item in purchases
    )
    derived_custom = any(
        _belongs_to_operation(item.id.removeprefix("custom:"), operation_id, "custom")
        for item in custom_items
    )
    if derived_purchase or derived_custom:
        raise InconsistentPhotoOperation(
            "This photo operation is missing its ledger commit and must be reviewed."
        )
    return None


def _belongs_to_operation(candidate: str, operation_id: str, kind: str) -> bool:
    # UUID5 values cannot be reversed. Check the bounded supported coordinate
    # space instead (3 segments x 30 source lines).
    for segment in range(3):
        for line in range(31):
            expected = (
                deterministic_purchase_event_id(operation_id, segment, line)
                if kind == "purchase"
                else deterministic_custom_pantry_id(operation_id, segment, line).removeprefix("custom:")
            )
            if candidate == expected:
                return True
    return False


def commit_photo_import(
    state,
    *,
    operation_id: str,
    kind: PhotoKind,
    images: Sequence[NormalizedImage],
    purchase_inputs: Sequence[PurchaseInput],
    custom_items: Sequence[CustomPantryItem] = (),
    transaction_fingerprint: str | None = None,
    now: datetime | None = None,
) -> PhotoImportCommitResult:
    """Commit Pantry, purchases, Plan cache, ledger, and image references once."""

    previous = existing_operation(
        operation_id,
        state.photo_imports,
        state.purchase_log,
        state.pantry.custom_items,
    )
    if previous is not None:
        return PhotoImportCommitResult(previous, replayed=True)

    if kind is PhotoKind.RECEIPT and not images:
        raise ValueError("A receipt import requires at least one sanitized image.")
    if kind is PhotoKind.PRODUCT and len(images) != 1:
        raise ValueError("A product import requires exactly one sanitized image.")

    imported_images = tuple(
        _image_record(operation_id, segment_index, image)
        for segment_index, image in enumerate(images)
    )
    image_by_segment = {image.segment_index: image.local_path for image in imported_images}
    prepared_inputs: list[PurchaseInput] = []
    for purchase_input in purchase_inputs:
        segment = purchase_input.segment_index or 0
        source = purchase_input.source_line_index or 0
        expected = deterministic_purchase_event_id(operation_id, segment, source)
        if purchase_input.event_id != expected:
            raise ValueError("Photo purchase event ID is not deterministic.")
        prepared_inputs.append(replace(
            purchase_input,
            photo_path=image_by_segment.get(segment),
            group_id=purchase_input.group_id or operation_id,
        ))
    for item in custom_items:
        # Product-derived custom items use the uploaded product photo. Receipt
        # custom items keep a separately selected licensed/uploaded image.
        if kind is PhotoKind.PRODUCT and not item.image_path:
            item.image_path = imported_images[0].local_path
            item.image_source = "uploaded_product"

    final_paths = [state.store.base_dir / image.local_path for image in imported_images]
    written_paths: list = []
    try:
        for normalized, final_path in zip(images, final_paths):
            final_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = final_path.parent / f".tmp-{final_path.name}"
            temporary.write_bytes(normalized.content)
            os.replace(temporary, final_path)
            written_paths.append(final_path)
    except OSError as exc:
        for path in written_paths:
            try:
                path.unlink()
            except OSError:
                pass
        raise RuntimeError("The imported image could not be saved.") from exc

    plan = state.saved_plan
    snapshot_items = dict(state.pantry.items)
    snapshot_custom = list(state.pantry.custom_items)
    snapshot_purchased = dict(plan.purchased) if plan is not None else None
    snapshot_log = list(state.purchase_log)
    snapshot_ledger = list(state.photo_imports)
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    record = PhotoImportRecord(
        operation_id=operation_id,
        photo_kind=kind,
        imported_at=stamp,
        images=imported_images,
        transaction_fingerprint=transaction_fingerprint,
        purchase_event_ids=tuple(item.event_id for item in prepared_inputs),
        custom_pantry_ids=tuple(item.id for item in custom_items),
    )
    try:
        record_purchase_events(plan, state.pantry, state.purchase_log, prepared_inputs, now=now)
        for item in custom_items:
            if state.pantry.custom_item(item.id) is not None:
                raise InconsistentPhotoOperation("A Custom Pantry ID already exists.")
            state.pantry.add_custom_item(item)
        state.photo_imports.append(record)
        applied = any(item.apply_to_plan for item in prepared_inputs)
        state.persist(
            plan=plan if applied and plan is not None else None,
            pantry=state.pantry,
            purchases=state.purchase_log,
            photo_imports=state.photo_imports,
        )
    except Exception:
        state.pantry.items.clear()
        state.pantry.items.update(snapshot_items)
        state.pantry.custom_items[:] = snapshot_custom
        if plan is not None and snapshot_purchased is not None:
            plan.purchased.clear()
            plan.purchased.update(snapshot_purchased)
        state.purchase_log[:] = snapshot_log
        state.photo_imports[:] = snapshot_ledger
        for path in written_paths:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return PhotoImportCommitResult(record, replayed=False)


def _image_record(
    operation_id: str,
    segment_index: int,
    image: NormalizedImage,
):
    from models.photo_import import ImportedImage

    return ImportedImage(
        sha256=image.sha256,
        local_path=(
            f"{IMPORTED_IMAGES_DIRNAME}/{operation_id}-{segment_index}{image.extension}"
        ),
        segment_index=segment_index,
    )
