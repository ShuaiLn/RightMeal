"""Pure duplicate, fingerprint, and idempotency rules for photo imports."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from models.pantry import MAPPING_LINKED, CustomPantryItem, Pantry
from models.photo_analysis import (
    PhotoKind,
    ReceiptFacts,
    ReceiptLineClassification,
    ReceiptScanFacts,
    ReceiptScanItemKind,
)
from models.photo_import import PhotoImportRecord
from models.plan import RIGHTMEAL_NS
from models.purchase_log import PurchaseRecord
from models.purchase_log import PurchaseInput
from models.quantities import (
    canonical_grams,
    canonical_money,
    canonical_quantity,
)
from services.pantry_flow import record_purchase_events
from services.photo_images import NormalizedImage
from services.photo_import_store import IMPORTED_IMAGES_DIRNAME
from services.tx import TransactionRecoveryRequiredError


# Serializes the complete import critical section even when two AppState
# instances point at the same profile.  The per-state TransactionManager lock is
# also held while writing so all ordinary persistence remains serialized.
_PHOTO_IMPORT_LOCK = threading.RLock()


@dataclass(frozen=True)
class DuplicateImportCheck:
    blocked: bool
    requires_confirmation: bool
    previous_import_at: str | None
    message: str | None
    previous_operation_id: str | None = None
    matched_image_hash: str | None = None


class InconsistentPhotoOperation(RuntimeError):
    pass


class StalePhotoImportContext(InconsistentPhotoOperation):
    pass


class DuplicatePhotoImport(InconsistentPhotoOperation):
    pass


@dataclass(frozen=True)
class PhotoDialogContext:
    operation_id: str
    analysis_id: int
    plan_id: str | None
    plan_revision: int
    pantry_revision: int
    purchase_revision: int
    photo_import_revision: int
    catalog_package_signature: str
    price_offer_signature: str

    @property
    def catalog_signature(self) -> str:
        """Compatibility alias for callers that used the old narrower name."""

        return self.catalog_package_signature


@dataclass(frozen=True)
class DuplicateAcknowledgement:
    operation_id: str
    previous_operation_id: str
    # ``None`` means the receipt matched by transaction fingerprint rather
    # than by reusing the exact same sanitized image.
    image_hash: str | None
    ledger_revision: int


@dataclass(frozen=True)
class PhotoImportCommand:
    """Immutable snapshot of exactly what the user confirmed.

    Raw product/receipt names may remain in the frozen PurchaseInput/custom
    payload for audit display, but ``commit_fingerprint`` deliberately hashes
    only image/target/destination/food/package/quantity/unit/grams fields.
    """

    operation_id: str
    kind: PhotoKind
    image_hashes: tuple[str, ...]
    plan_id: str | None
    context: PhotoDialogContext
    purchase_inputs: tuple[PurchaseInput, ...] = ()
    custom_item_payloads: tuple[str, ...] = ()
    purchase_units: tuple[tuple[str, str], ...] = ()
    transaction_fingerprint: str | None = None
    duplicate_acknowledgement: DuplicateAcknowledgement | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or self.context.operation_id != self.operation_id:
            raise ValueError("photo command and dialog operation IDs must match")
        if self.plan_id != self.context.plan_id:
            raise ValueError("photo command target Plan differs from its dialog context")
        if self.kind not in (PhotoKind.PRODUCT, PhotoKind.RECEIPT):
            raise ValueError("only product and receipt imports can be committed")
        if (
            self.duplicate_acknowledgement is not None
            and self.duplicate_acknowledgement.operation_id != self.operation_id
        ):
            raise ValueError("duplicate acknowledgement belongs to another operation")
        if not self.image_hashes or any(not _valid_hash(value) for value in self.image_hashes):
            raise ValueError("photo command needs valid sanitized image hashes")
        units = dict(self.purchase_units)
        if len(units) != len(self.purchase_units):
            raise ValueError("purchase units must be unique per purchase event")
        purchase_ids = {item.event_id for item in self.purchase_inputs}
        if set(units) != purchase_ids or any(not value.strip() for value in units.values()):
            raise ValueError("every catalog purchase needs one explicit unit")
        if len(purchase_ids) != len(self.purchase_inputs):
            raise ValueError("photo purchase event IDs must be unique")
        custom_ids = [item.id for item in self.custom_items()]
        if len(set(custom_ids)) != len(custom_ids):
            raise ValueError("photo Custom Pantry IDs must be unique")
        if not self.purchase_inputs and not self.custom_item_payloads:
            raise ValueError("a photo command must contain at least one destination item")

    @classmethod
    def create(
        cls,
        *,
        operation_id: str,
        kind: PhotoKind,
        images: Sequence[NormalizedImage],
        plan_id: str | None,
        context: PhotoDialogContext,
        purchase_inputs: Sequence[PurchaseInput] = (),
        custom_items: Sequence[CustomPantryItem] = (),
        purchase_units: Mapping[str, str] | None = None,
        transaction_fingerprint: str | None = None,
        duplicate_acknowledgement: DuplicateAcknowledgement | None = None,
    ) -> "PhotoImportCommand":
        units = dict(purchase_units or {})
        return cls(
            operation_id=operation_id,
            kind=kind,
            image_hashes=tuple(image.sha256 for image in images),
            plan_id=plan_id,
            context=context,
            purchase_inputs=tuple(purchase_inputs),
            custom_item_payloads=tuple(
                json.dumps(item.to_dict(), sort_keys=True, separators=(",", ":"))
                for item in custom_items
            ),
            purchase_units=tuple(sorted(
                (event_id, unit.strip()) for event_id, unit in units.items()
            )),
            transaction_fingerprint=transaction_fingerprint,
            duplicate_acknowledgement=duplicate_acknowledgement,
        )

    def custom_items(self) -> tuple[CustomPantryItem, ...]:
        values: list[CustomPantryItem] = []
        for payload in self.custom_item_payloads:
            raw = json.loads(payload)
            item = CustomPantryItem.from_dict(raw)
            if item is None:
                raise ValueError("photo command contains an invalid Custom Pantry item")
            if not item.unit.strip():
                raise ValueError("every Custom Pantry item needs its original unit")
            values.append(item)
        return tuple(values)

    @property
    def commit_fingerprint(self) -> str:
        units = dict(self.purchase_units)
        items: list[dict[str, str | None]] = []
        for purchase in self.purchase_inputs:
            items.append({
                "destination": "plan" if purchase.apply_to_plan else "pantry",
                "food_id": purchase.food_id,
                "package": purchase.package_id or purchase.package_label,
                "quantity": canonical_quantity(purchase.quantity),
                "unit": " ".join(units[purchase.event_id].casefold().split()),
                "grams": canonical_grams(purchase.grams),
            })
        for custom in self.custom_items():
            items.append({
                "destination": "custom",
                "food_id": None,
                "package": None,
                "quantity": canonical_quantity(custom.amount),
                "unit": " ".join(custom.unit.casefold().split()),
                "grams": canonical_grams(custom.grams_estimate),
            })
        payload = {
            "kind": self.kind.value,
            "image_hashes": list(self.image_hashes),
            "plan_id": self.plan_id,
            "items": sorted(
                items,
                key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
            ),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DialogContextValidation:
    valid: bool
    rerun_matcher: bool
    message: str | None
    close_dialog: bool = False
    rerun_duplicate_check: bool = False


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
    current_plan_revision: int,
    current_pantry_revision: int,
    current_purchase_revision: int,
    current_photo_import_revision: int,
    current_catalog_package_signature: str,
    current_price_offer_signature: str,
) -> DialogContextValidation:
    if context.analysis_id != current_analysis_id:
        return DialogContextValidation(
            False, False, "A newer photo analysis replaced this dialog.", close_dialog=True
        )
    if context.catalog_package_signature != current_catalog_package_signature:
        return DialogContextValidation(
            False, True, "The catalog changed; candidates were refreshed. Confirm again."
        )
    if (
        context.plan_id != current_plan_id
        or context.plan_revision != current_plan_revision
    ):
        return DialogContextValidation(
            False, False, "The current Plan changed; destinations were refreshed. Confirm again."
        )
    if (
        context.pantry_revision != current_pantry_revision
        or context.purchase_revision != current_purchase_revision
    ):
        return DialogContextValidation(
            False,
            False,
            "Pantry or purchase history changed; amounts and destinations were refreshed. Confirm again.",
        )
    if context.price_offer_signature != current_price_offer_signature:
        return DialogContextValidation(
            False,
            False,
            "Available package prices changed; review the refreshed result and confirm again.",
        )
    if context.photo_import_revision != current_photo_import_revision:
        return DialogContextValidation(
            False,
            False,
            "Photo import history changed; duplicate status was refreshed. Confirm again.",
            rerun_duplicate_check=True,
        )
    return DialogContextValidation(True, False, None)


def catalog_package_signature(state) -> str:
    payload = []
    for food in sorted(state.foods, key=lambda value: value.id):
        payload.append({
            "id": food.id,
            "name": food.name,
            "form": food.form,
            "density": food.density_g_per_ml,
            "packages": [
                {
                    "label": package.label,
                    "package_id": getattr(package, "package_id", None),
                    "grams": canonical_grams(package.grams),
                    "ml": canonical_grams(package.ml) if package.ml is not None else None,
                    "seed_price": canonical_money(package.seed_price),
                }
                for package in food.package_options
            ],
        })
    matcher = getattr(state, "pantry_matcher", None)
    encoded = json.dumps({
        "catalog": payload,
        "matcher": getattr(matcher, "signature", None),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def price_offer_signature(state) -> str:
    plan = state.saved_plan
    if plan is None:
        return hashlib.sha256(b"no-plan-offers").hexdigest()
    fields = (
        "basket_item_id", "food_id", "package_id", "package_label", "count",
        "offer_id", "unit_cost", "cost", "total_cost", "source", "store",
    )
    offers = [
        {name: getattr(item, name, None) for name in fields}
        for item in plan.basket
    ]
    encoded = json.dumps(offers, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dialog_context_for_state(
    state,
    *,
    operation_id: str,
    analysis_id: int,
) -> PhotoDialogContext:
    return PhotoDialogContext(
        operation_id=operation_id,
        analysis_id=analysis_id,
        plan_id=state.saved_plan.plan_id if state.saved_plan else None,
        plan_revision=state.plan_revision,
        pantry_revision=state.pantry_revision,
        purchase_revision=state.purchase_revision,
        photo_import_revision=state.photo_import_revision,
        catalog_package_signature=catalog_package_signature(state),
        price_offer_signature=price_offer_signature(state),
    )


def validate_context_against_state(
    context: PhotoDialogContext,
    state,
) -> DialogContextValidation:
    return validate_dialog_context(
        context,
        current_analysis_id=state.photo_analysis_seq,
        current_plan_id=state.saved_plan.plan_id if state.saved_plan else None,
        current_plan_revision=state.plan_revision,
        current_pantry_revision=state.pantry_revision,
        current_purchase_revision=state.purchase_revision,
        current_photo_import_revision=state.photo_import_revision,
        current_catalog_package_signature=catalog_package_signature(state),
        current_price_offer_signature=price_offer_signature(state),
    )


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


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


def receipt_transaction_fingerprint(
    receipt: ReceiptFacts | ReceiptScanFacts,
) -> str | None:
    """Hash a reliable transaction summary; never store its raw components."""

    if isinstance(receipt, ReceiptScanFacts):
        totals = [
            canonical_money(item.printed_line_total)
            for item in receipt.items
            if item.kind in (ReceiptScanItemKind.FOOD, ReceiptScanItemKind.UNKNOWN)
            and not item.possible_duplicate
            and item.printed_line_total is not None
            and item.printed_line_total > 0
        ]
    else:
        totals = [
            canonical_money(line.printed_line_total)
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
        # Use canonical priced merchandise items, not the model's auxiliary
        # visible-line estimate, so re-photographing the same receipt cannot
        # evade the permanent duplicate check through estimate drift.
        "line_count": str(len(totals)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def check_duplicate_import(
    kind: PhotoKind,
    image_hashes: Sequence[str],
    transaction_fingerprint: str | None,
    ledger: Sequence[PhotoImportRecord],
    *,
    purchases: Sequence[PurchaseRecord] = (),
    pantry: Pantry | None = None,
    exclude_operation_id: str | None = None,
) -> DuplicateImportCheck:
    hashes = set(image_hashes)
    for record in reversed(ledger):
        if record.operation_id == exclude_operation_id:
            continue
        hash_match = bool(hashes.intersection(image.sha256 for image in record.images))
        fingerprint_match = bool(
            transaction_fingerprint
            and record.transaction_fingerprint == transaction_fingerprint
        )
        if not hash_match and not fingerprint_match:
            continue
        display_date = _display_import_date(record.imported_at)
        if kind is PhotoKind.RECEIPT:
            if record.photo_kind is not PhotoKind.RECEIPT:
                continue
            # Receipt history is retained for audit, but it should not nag the
            # user after every Pantry output created by that import is gone.
            # ``pantry=None`` keeps fail-closed behavior for legacy pure callers;
            # all production calls provide the current Pantry.
            if pantry is not None and not _import_has_active_output(
                record, purchases, pantry
            ):
                continue
            return DuplicateImportCheck(
                blocked=pantry is None,
                requires_confirmation=pantry is not None,
                previous_import_at=record.imported_at,
                message=(
                    f"This receipt was already imported on {display_date}."
                    if pantry is None else
                    f"This receipt was imported on {display_date}, and some of its "
                    "Pantry items still remain. Add it anyway?"
                ),
                previous_operation_id=record.operation_id,
                matched_image_hash=next(
                    (image.sha256 for image in record.images if image.sha256 in hashes),
                    None,
                ),
            )
        # Product warnings describe an active output, not immutable history.
        # ``pantry=None`` retains compatibility for legacy pure callers; every
        # production call supplies the current Pantry explicitly.
        if record.photo_kind is not PhotoKind.PRODUCT:
            continue
        if pantry is not None and not _import_has_active_output(
            record, purchases, pantry
        ):
            continue
        return DuplicateImportCheck(
            blocked=False,
            requires_confirmation=True,
            previous_import_at=record.imported_at,
            message=f"This image was imported on {display_date}. Continue anyway?",
            previous_operation_id=record.operation_id,
            matched_image_hash=next(
                (image.sha256 for image in record.images if image.sha256 in hashes),
                None,
            ),
        )
    return DuplicateImportCheck(False, False, None, None)


def _import_has_active_output(
    record: PhotoImportRecord,
    purchases: Sequence[PurchaseRecord],
    pantry: Pantry,
) -> bool:
    purchase_by_id = {item.event_id: item for item in purchases}
    if any(
        (purchase := purchase_by_id.get(event_id)) is not None
        and purchase.voided_at is None
        and pantry.items.get(purchase.food_id, 0.0) > 0
        for event_id in record.purchase_event_ids
    ):
        return True
    custom_by_id = {item.id: item for item in pantry.custom_items}
    for item_id in record.custom_pantry_ids:
        custom = custom_by_id.get(item_id)
        if custom is None:
            continue
        if custom.mapping_status != MAPPING_LINKED:
            return True
        if (
            custom.canonical_food_id
            and pantry.items.get(custom.canonical_food_id, 0.0) > 0
        ):
            return True
    return False


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
    *,
    expected_fingerprint: str | None = None,
    expected_image_hashes: Sequence[str] = (),
    expected_purchase_ids: Sequence[str] | None = None,
    expected_custom_ids: Sequence[str] | None = None,
    base_dir: Path | None = None,
) -> PhotoImportRecord | None:
    """Return a fully committed retry, or block a partially present operation."""

    record = next((item for item in ledger if item.operation_id == operation_id), None)
    purchase_ids = {item.event_id for item in purchases}
    custom_ids = {item.id for item in custom_items}
    if record is not None:
        if expected_fingerprint is None or record.commit_fingerprint != expected_fingerprint:
            raise InconsistentPhotoOperation(
                "This operation ID belongs to a different or legacy photo command."
            )
        recorded_hashes = tuple(
            image.sha256 for image in sorted(record.images, key=lambda value: value.segment_index)
        )
        if tuple(expected_image_hashes) != recorded_hashes:
            raise InconsistentPhotoOperation(
                "This operation ID was committed with different sanitized images."
            )
        if (
            expected_purchase_ids is not None
            and tuple(record.purchase_event_ids) != tuple(expected_purchase_ids)
        ) or (
            expected_custom_ids is not None
            and tuple(record.custom_pantry_ids) != tuple(expected_custom_ids)
        ):
            raise InconsistentPhotoOperation(
                "This operation has an incomplete or mismatched child-record manifest."
            )
        purchases_complete = set(record.purchase_event_ids).issubset(purchase_ids)
        customs_complete = set(record.custom_pantry_ids).issubset(custom_ids)
        images_complete = bool(base_dir is not None) and all(
            _committed_image_is_complete(base_dir, image.local_path, image.sha256)
            for image in record.images
        )
        if purchases_complete and customs_complete and images_complete:
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


def _committed_image_is_complete(base_dir: Path, relative: str, expected_hash: str) -> bool:
    try:
        path = (Path(base_dir) / relative).resolve()
        root = Path(base_dir).resolve()
        path.relative_to(root)
        content = path.read_bytes()
    except (OSError, ValueError):
        return False
    return hashlib.sha256(content).hexdigest() == expected_hash


def _belongs_to_operation(candidate: str, operation_id: str, kind: str) -> bool:
    # UUID5 values cannot be reversed. Check the bounded supported receipt
    # space instead (5 images x 30 ordered items).
    for segment in range(5):
        for line in range(30):
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
    command: PhotoImportCommand | None = None,
    images: Sequence[NormalizedImage],
    operation_id: str | None = None,
    kind: PhotoKind | None = None,
    purchase_inputs: Sequence[PurchaseInput] = (),
    custom_items: Sequence[CustomPantryItem] = (),
    purchase_units: Mapping[str, str] | None = None,
    transaction_fingerprint: str | None = None,
    duplicate_acknowledgement: DuplicateAcknowledgement | None = None,
    now: datetime | None = None,
) -> PhotoImportCommitResult:
    """Commit one immutable command under the complete import critical section."""

    if command is None:
        if operation_id is None or kind is None:
            raise TypeError("operation_id and kind are required without a command")
        context = dialog_context_for_state(
            state,
            operation_id=operation_id,
            analysis_id=state.photo_analysis_seq,
        )
        command = PhotoImportCommand.create(
            operation_id=operation_id,
            kind=kind,
            images=images,
            plan_id=context.plan_id,
            context=context,
            purchase_inputs=purchase_inputs,
            custom_items=custom_items,
            purchase_units=purchase_units,
            transaction_fingerprint=transaction_fingerprint,
            duplicate_acknowledgement=duplicate_acknowledgement,
        )
    elif any((operation_id is not None, kind is not None, purchase_inputs, custom_items)):
        raise TypeError("pass either an immutable command or legacy command fields, not both")

    if tuple(image.sha256 for image in images) != command.image_hashes:
        raise InconsistentPhotoOperation("Sanitized images differ from the confirmed command.")
    if command.kind is PhotoKind.RECEIPT and len(images) > 5:
        raise ValueError("A receipt import supports at most five segments.")
    if command.kind is PhotoKind.PRODUCT and len(images) != 1:
        raise ValueError("A product import requires exactly one sanitized image.")

    with _PHOTO_IMPORT_LOCK, state.tx.lock:
        # Replay is a read-only result and remains valid after the successful
        # transaction advanced revisions.  Mismatch or incomplete children fail
        # before any optimistic-context check or write.
        previous = existing_operation(
            command.operation_id,
            state.photo_imports,
            state.purchase_log,
            state.pantry.custom_items,
            expected_fingerprint=command.commit_fingerprint,
            expected_image_hashes=command.image_hashes,
            expected_purchase_ids=tuple(
                item.event_id for item in command.purchase_inputs
            ),
            expected_custom_ids=tuple(
                item.id for item in command.custom_items()
            ),
            base_dir=state.store.base_dir,
        )
        if previous is not None:
            return PhotoImportCommitResult(previous, replayed=True)

        context_validation = validate_context_against_state(command.context, state)
        if not context_validation.valid:
            raise StalePhotoImportContext(
                context_validation.message or "The photo confirmation is stale."
            )
        if state.tx.writes_frozen:
            # Raises the TransactionManager's authoritative recovery result.
            state.tx.save_all({})  # pragma: no cover - always raises while frozen

        duplicate = check_duplicate_import(
            command.kind,
            command.image_hashes,
            command.transaction_fingerprint,
            state.photo_imports,
            purchases=state.purchase_log,
            pantry=state.pantry,
            exclude_operation_id=command.operation_id,
        )
        if duplicate.blocked:
            raise DuplicatePhotoImport(
                duplicate.message or "This receipt was already imported."
            )
        if duplicate.requires_confirmation:
            acknowledgement = command.duplicate_acknowledgement
            if (
                acknowledgement is None
                or acknowledgement.operation_id != command.operation_id
                or acknowledgement.previous_operation_id != duplicate.previous_operation_id
                or acknowledgement.image_hash != duplicate.matched_image_hash
                or acknowledgement.ledger_revision != state.photo_import_revision
            ):
                raise StalePhotoImportContext(
                    "Duplicate status changed; review it and confirm again."
                )

        imported_images = tuple(
            _image_record(command.operation_id, segment_index, image)
            for segment_index, image in enumerate(images)
        )
        image_by_segment = {
            image.segment_index: image.local_path for image in imported_images
        }
        prepared_inputs: list[PurchaseInput] = []
        for purchase_input in command.purchase_inputs:
            segment = purchase_input.segment_index or 0
            source = purchase_input.source_line_index or 0
            if not 0 <= segment < len(imported_images):
                raise ValueError("Photo purchase segment index is outside the session.")
            expected = deterministic_purchase_event_id(
                command.operation_id, segment, source
            )
            if purchase_input.event_id != expected:
                raise ValueError("Photo purchase event ID is not deterministic.")
            food = state.foods_by_id.get(purchase_input.food_id)
            if food is None:
                raise StalePhotoImportContext(
                    "The selected catalog food no longer exists; confirm again."
                )
            selected_packages = [
                package for package in food.package_options
                if (
                    purchase_input.package_id
                    and package.package_id == purchase_input.package_id
                ) or (
                    not purchase_input.package_id
                    and purchase_input.package_label
                    and package.label == purchase_input.package_label
                )
            ]
            if (
                (purchase_input.package_id or purchase_input.package_label)
                and len(selected_packages) != 1
            ):
                raise StalePhotoImportContext(
                    "The selected package no longer belongs to this food; confirm again."
                )
            if purchase_input.apply_to_plan and (
                state.saved_plan is None
                or state.saved_plan.plan_id != command.plan_id
            ):
                raise StalePhotoImportContext(
                    "The target Plan is no longer available; confirm again."
                )
            prepared_inputs.append(replace(
                purchase_input,
                photo_path=image_by_segment[segment],
                group_id=purchase_input.group_id or command.operation_id,
            ))

        prepared_custom = list(command.custom_items())
        for item in prepared_custom:
            if command.kind is PhotoKind.PRODUCT and not item.image_path:
                item.image_path = imported_images[0].local_path
                item.image_source = "uploaded_product"

        final_paths = [
            state.store.base_dir / image.local_path for image in imported_images
        ]
        if any(path.exists() for path in final_paths):
            raise InconsistentPhotoOperation(
                "This photo operation has image files without a complete ledger commit."
            )
        written_paths: list[Path] = []
        try:
            for normalized, final_path in zip(images, final_paths):
                final_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = final_path.parent / f".tmp-{final_path.name}"
                temporary.write_bytes(normalized.content)
                os.replace(temporary, final_path)
                written_paths.append(final_path)
        except OSError as exc:
            _remove_paths(written_paths)
            raise RuntimeError("The imported image could not be saved.") from exc

        plan = state.saved_plan
        snapshot_items = dict(state.pantry.items)
        snapshot_custom = list(state.pantry.custom_items)
        snapshot_purchased = dict(plan.purchased) if plan is not None else None
        snapshot_log = list(state.purchase_log)
        snapshot_ledger = list(state.photo_imports)
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        record = PhotoImportRecord(
            operation_id=command.operation_id,
            photo_kind=command.kind,
            imported_at=stamp,
            images=imported_images,
            transaction_fingerprint=command.transaction_fingerprint,
            purchase_event_ids=tuple(item.event_id for item in prepared_inputs),
            custom_pantry_ids=tuple(item.id for item in prepared_custom),
            commit_fingerprint=command.commit_fingerprint,
        )
        try:
            record_purchase_events(
                plan, state.pantry, state.purchase_log, prepared_inputs, now=now
            )
            for item in prepared_custom:
                if state.pantry.custom_item(item.id) is not None:
                    raise InconsistentPhotoOperation(
                        "A Custom Pantry ID already exists."
                    )
                state.pantry.add_custom_item(item)
            state.photo_imports.append(record)
            applied = any(item.apply_to_plan for item in prepared_inputs)
            persist_kwargs = {
                "plan": plan if applied and plan is not None else None,
                "pantry": state.pantry,
                "purchases": state.purchase_log if prepared_inputs else None,
                "photo_imports": state.photo_imports,
            }
            state.persist(**persist_kwargs)
        except TransactionRecoveryRequiredError:
            # Disk may contain the committed snapshot or a partial snapshot and
            # the journal is the only recovery authority.  Preserve matching
            # memory/images and freeze further writes; never claim rollback.
            state.photo_import_error = (
                "Photo import recovery is required; writes are paused."
            )
            raise
        except Exception:
            state.pantry.items.clear()
            state.pantry.items.update(snapshot_items)
            state.pantry.custom_items[:] = snapshot_custom
            if plan is not None and snapshot_purchased is not None:
                plan.purchased.clear()
                plan.purchased.update(snapshot_purchased)
            state.purchase_log[:] = snapshot_log
            state.photo_imports[:] = snapshot_ledger
            _remove_paths(written_paths)
            raise
        return PhotoImportCommitResult(record, replayed=False)


def _remove_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


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
