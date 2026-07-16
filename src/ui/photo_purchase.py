"""Shared evidence-only product-photo and receipt import flows."""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

import flet as ft

import theme
from models.pantry import CustomPantryItem
from models.food import PackageUnit
from models.photo_analysis import (
    PhotoKind,
    ProductFacts,
    ReceiptBarrierKind,
    ReceiptFacts,
    ReceiptLineClassification,
    ReceiptLineFacts,
    ReceiptScanFacts,
    ReceiptScanItem,
    ReceiptScanItemKind,
)
from models.purchase_log import (
    ORIGIN_PRODUCT_PHOTO,
    ORIGIN_RECEIPT,
    PRICE_SOURCE_RECEIPT,
    PRICE_SOURCE_USER,
    PRICE_SOURCE_UNKNOWN,
    PRICE_SOURCE_VISIBLE,
    PurchaseInput,
)
from models.quantities import normalize_grams, normalize_money, normalize_quantity
from services.pantry_matcher import CatalogMatcher, MatchResult
from services.photo_analyzer import (
    AnalyzedPhoto,
    AnalyzedReceiptSegment,
    ReceiptScanError,
    get_photo_analyzer,
)
from services.photo_images import crop_region, crop_to_boundary
from services.photo_imports import (
    DuplicateAcknowledgement,
    DuplicatePhotoImport,
    PhotoImportCommand,
    PhotoDialogContext,
    StalePhotoImportContext,
    check_duplicate_import,
    commit_photo_import,
    deterministic_custom_pantry_id,
    deterministic_purchase_event_id,
    dialog_context_for_state,
    new_operation_id,
    receipt_transaction_fingerprint,
    validate_context_against_state,
)
from services.photo_resolution import (
    GRAMS_SOURCE_CATALOG_ESTIMATE,
    GRAMS_SOURCE_USER_ENTERED,
    confirmed_item_spend,
    confirmed_line_total,
    convert_to_grams,
    matching_packages,
    resolve_weight,
)
from services.package_units import (
    format_grams,
    package_unit,
    package_unit_name,
    plan_package_units,
    recent_purchase_package_unit,
)
from services.receipt_validation import (
    ReceiptSessionStatus,
    combine_receipt_segments,
    confirm_manual_boundary,
    rebase_receipt_to_boundary_crop,
    receipt_session_status,
    validate_receipt_coverage,
)
from services.receipt_scanning import combine_receipt_scans
from services.source_allocation import allocate_sources, is_historical
from services.tx import TransactionRecoveryRequiredError
from services.units import parse_size, to_grams
from ui.components import food_avatar, muted_text, style_field
from ui.image_processing import ImageFailureDetails, ImageProcessingView
from ui.state import AppState

ACTION_APPLY = "apply"
ACTION_PANTRY = "pantry"
ACTION_CUSTOM = "custom"
ACTION_IGNORE = "ignore"


def _canonical_unit_name(value: str | None) -> str:
    normalized = " ".join((value or "").strip().casefold().split())
    aliases = {
        "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg",
        "ounce": "oz", "ounces": "oz", "pound": "lb", "pounds": "lb",
        "lbs": "lb", "milliliter": "ml", "milliliters": "ml",
        "liter": "l", "liters": "l", "fluid ounce": "fl oz",
        "fluid ounces": "fl oz", "doz": "dozen", "ea": "each",
        "count": "each", "ct": "each",
    }
    return aliases.get(normalized, normalized)


def _evidence_amount_unit(facts, food, resolved) -> tuple[float, str, float] | None:
    """Return a real observed/derived amount; never invent one gram."""

    density = food.density_g_per_ml if food is not None else None
    if facts.total_weight is not None:
        unit = _canonical_unit_name(facts.total_weight.unit)
        grams = convert_to_grams(
            facts.total_weight.value,
            unit,
            density_g_per_ml=density,
        )
        if grams is not None:
            return facts.total_weight.value, unit, grams
    if facts.unit_weight is not None and facts.quantity is not None:
        unit = _canonical_unit_name(facts.unit_weight.unit)
        amount = facts.unit_weight.value * facts.quantity
        grams = convert_to_grams(amount, unit, density_g_per_ml=density)
        if grams is not None:
            return amount, unit, grams
    description = (
        " ".join(filter(None, [facts.package_text, facts.observed_name]))
        if isinstance(facts, ProductFacts)
        else facts.raw_printed_text
    )
    parsed = parse_size(description)
    if parsed is not None:
        amount, raw_unit = parsed
        unit = _canonical_unit_name(raw_unit)
        grams = (
            to_grams(amount, unit, food)
            if food is not None else convert_to_grams(amount, unit)
        )
        if grams is not None:
            return amount, unit, grams
    if resolved.grams is not None:
        # The local resolver may have safely combined a multipack expression.
        return resolved.grams, "g", resolved.grams
    return None


def _show_message(page: ft.Page, message: str) -> None:
    page.show_dialog(ft.SnackBar(ft.Text(message)))


_pickers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def ensure_file_picker(page: ft.Page) -> ft.FilePicker:
    picker = ft.FilePicker()
    _pickers[page] = picker
    return picker


async def _pick_images(
    page: ft.Page,
    picker: ft.FilePicker,
    title: str,
    *,
    allow_multiple: bool,
    processing_view: ImageProcessingView | None = None,
) -> list[bytes] | None:
    files = await picker.pick_files(
        dialog_title=title,
        allowed_extensions=["jpg", "jpeg", "png"],
        allow_multiple=allow_multiple,
    )
    if not files:
        return None
    if processing_view is not None:
        processing_view.show(
            "Preparing the uploaded image" if len(files) == 1
            else f"Preparing {len(files)} uploaded images",
            "The files are being read, validated, and prepared for analysis.",
        )
    if len(files) > 5:
        if processing_view is not None:
            processing_view.show_failure(ImageFailureDetails(
                summary="Too many images were selected for one receipt import.",
                stage="File selection",
                reason="A receipt session supports no more than five images.",
                suggestions=("Select one to five images in top-to-bottom order.",),
            ))
        else:
            _show_message(page, "Choose no more than five overlapping receipt photos.")
        return None
    selected: list[bytes] = []
    for file in files:
        if not file.path:
            if processing_view is not None:
                processing_view.show_failure(ImageFailureDetails(
                    summary="The selected image is not available to the application.",
                    stage="File access",
                    reason="The file picker did not provide a readable local path.",
                    suggestions=("Choose the image again from a local folder.",),
                ))
            return None
        try:
            selected.append(Path(file.path).read_bytes())
        except OSError:
            if processing_view is not None:
                processing_view.show_failure(ImageFailureDetails(
                    summary="One of the selected image files could not be opened.",
                    stage="File access",
                    reason="The file may have moved, be locked, or be unreadable.",
                    suggestions=(
                        "Choose the image again.",
                        "Copy it to a local folder and retry.",
                    ),
                ))
            else:
                _show_message(page, "One of the selected files could not be read.")
            return None
    return selected


def _receipt_failure_diagnostics(analyzed: AnalyzedPhoto) -> tuple[tuple[str, str], ...]:
    diagnostics = analyzed.diagnostics
    if diagnostics is None:
        return ()
    values = [
        ("Coordinate space", diagnostics.coordinate_space or "Not available"),
        ("Coordinate order", diagnostics.coordinate_order or "Not available"),
        ("Final image size", f"{diagnostics.image_width} × {diagnostics.image_height} px"),
        ("Failure stage", diagnostics.failure_stage or "Not available"),
        ("Automatic retry", "Completed" if diagnostics.retried else "Not needed"),
    ]
    if diagnostics.failure_line_index is not None:
        values.append(("Failed line index", str(diagnostics.failure_line_index)))
    if diagnostics.raw_merchandise_area is not None:
        values.append((
            "Reported merchandise area",
            ", ".join(f"{value:g}" for value in diagnostics.raw_merchandise_area),
        ))
    return tuple(values)


def _guarded_analyzer(page: ft.Page, state: AppState):
    if state.purchase_log_error or state.photo_import_error:
        _show_message(
            page,
            "Purchase or photo-import history could not be read; photo imports "
            "are paused to protect your data.",
        )
        return None
    analyzer = get_photo_analyzer(state.profile, state.http_client)
    if analyzer is None:
        _show_message(page, "Add an OpenAI key in Profile to analyze photos.")
    return analyzer


def _matcher(state: AppState) -> CatalogMatcher:
    if state.pantry_matcher is None or state.pantry_matcher.foods != state.foods:
        state.pantry_matcher = CatalogMatcher(
            state.foods,
            cache_dir=state.store.base_dir / "matching_cache",
        )
    return state.pantry_matcher


def _plan_food_ids(state: AppState) -> set[str]:
    if state.saved_plan is None:
        return set()
    return {item.food_id for item in state.saved_plan.basket}


def _candidate_options(state: AppState, result: MatchResult) -> list[ft.DropdownOption]:
    ordered = [candidate.food_id for candidate in result.candidates]
    ordered.extend(food.id for food in sorted(state.foods, key=lambda value: value.name))
    return [
        ft.DropdownOption(key=food_id, text=state.foods_by_id[food_id].name)
        for food_id in dict.fromkeys(ordered)
    ]


def _candidate_summary(
    state: AppState,
    result: MatchResult,
    limit: int = 3,
) -> ft.Column:
    rows: list[ft.Control] = []
    if result.status_message:
        rows.append(muted_text(result.status_message, size=11))
    for candidate in result.candidates[:limit]:
        food = state.foods_by_id[candidate.food_id]
        rows.append(ft.Row([
            food_avatar(food, size=30, image_src=state.image_src_for(food)),
            ft.Column([
                ft.Text(
                    f"{candidate.name} · {candidate.group} · {candidate.form} · "
                    f"match score {candidate.match_score:.2f}",
                    size=11,
                ),
                muted_text(candidate.reason, size=10.5),
            ], spacing=1, expand=True),
        ], spacing=7))
    return ft.Column(rows, spacing=3, tight=True)


def _default_apply(state: AppState, food_id: str | None) -> bool:
    plan = state.saved_plan
    if plan is None or is_historical(plan) or not food_id:
        return False
    allocation = allocate_sources(plan, state.pantry, state.foods_by_id).get(food_id)
    return bool(allocation and allocation.gap > 0)


def _context(
    state: AppState,
    operation_id: str,
    analysis_id: int,
    matcher: CatalogMatcher,
) -> PhotoDialogContext:
    # Ensure the cached matcher (and therefore its alias signature) is part of
    # the service-computed catalog/package snapshot.
    state.pantry_matcher = matcher
    return dialog_context_for_state(
        state,
        operation_id=operation_id,
        analysis_id=analysis_id,
    )


def _call_after_commit(
    page: ft.Page,
    on_committed: Callable[[], None],
) -> None:
    def refresh_now(e=None) -> None:
        del e
        try:
            on_committed()
        except Exception:
            page.show_dialog(ft.SnackBar(
                ft.Text("Import was saved, but the Pantry view could not be refreshed."),
                action="Refresh now",
                on_action=refresh_now,
                persist=True,
            ))

    try:
        on_committed()
    except Exception:
        page.show_dialog(ft.SnackBar(
            ft.Text("Import was saved, but the Pantry view could not be refreshed."),
            action="Refresh now",
            on_action=refresh_now,
            persist=True,
        ))


def open_product_confirm_dialog(
    page: ft.Page,
    state: AppState,
    analyzed: AnalyzedPhoto,
    on_committed: Callable[[], None],
    *,
    analysis_id: int,
    duplicate_acknowledgement: DuplicateAcknowledgement | None = None,
    operation_id: str | None = None,
) -> None:
    facts = analyzed.product
    if facts is None:
        return
    operation_id = operation_id or new_operation_id()
    local_matcher = _matcher(state)
    match = local_matcher.match(facts, plan_food_ids=_plan_food_ids(state))
    context_box = {"value": _context(state, operation_id, analysis_id, local_matcher)}
    def current_plan_live() -> bool:
        return state.saved_plan is not None and not is_historical(state.saved_plan)

    def purchase_units(food) -> tuple[tuple[PackageUnit, ...], PackageUnit | None]:
        if food is None:
            return (), None
        description = " ".join(
            filter(None, [facts.package_text, facts.observed_name])
        )
        explicit = matching_packages(description, food)
        explicit_unit = package_unit(food, explicit[0]) if len(explicit) == 1 else None
        planned = plan_package_units(
            food, state.saved_plan if current_plan_live() else None
        )
        recent = recent_purchase_package_unit(food, state.purchase_log)
        unique = (
            package_unit(food, food.package_options[0])
            if len(food.package_options) == 1 else None
        )
        ordered = [
            *((explicit_unit,) if explicit_unit is not None else ()),
            *planned,
            *((recent,) if recent is not None else ()),
            *((unique,) if unique is not None else ()),
        ]
        units = tuple(dict.fromkeys(ordered))
        if len(explicit) == 1:
            return units, explicit_unit
        if planned:
            return units, max(planned, key=lambda value: value.grams)
        if recent is not None:
            return units, recent
        return units, unique

    food_dropdown = ft.Dropdown(
        label="Local catalog match",
        width=330,
        editable=True,
        enable_filter=True,
        enable_search=True,
        menu_height=320,
        text_size=12.5,
        options=_candidate_options(state, match),
        value=match.selected_food_id,
    )
    name_field = ft.TextField(
        label="Observed product name",
        value=facts.observed_name or facts.generic_food_name,
        dense=True,
        text_size=12.5,
    )
    brand_field = ft.TextField(
        label="Brand",
        value=facts.brand or "",
        dense=True,
        text_size=12.5,
    )
    quantity_field = ft.TextField(
        label="Quantity / Amount *",
        value="",
        width=110,
        dense=True,
        text_size=12.5,
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    initial_food = state.foods_by_id.get(match.selected_food_id or "")
    resolved = resolve_weight(facts, initial_food)
    initial_units, initial_unit = purchase_units(initial_food)
    evidence_amount = _evidence_amount_unit(facts, initial_food, resolved)
    # Directly observed weight/packaging outranks Plan/recent/catalog defaults.
    if evidence_amount is not None and not (
        initial_unit is not None
        and initial_unit.package_label in " ".join(
            filter(None, [facts.package_text, facts.observed_name])
        )
    ):
        initial_unit = None
    initial_quantity = (
        normalize_quantity(facts.quantity or 1)
        if initial_unit is not None else evidence_amount[0] if evidence_amount else None
    )
    initial_grams = (
        initial_unit.to_grams(initial_quantity, initial_food)
        if initial_unit is not None and initial_food is not None and initial_quantity is not None
        else evidence_amount[2] if evidence_amount else None
    )
    quantity_field.value = f"{initial_quantity:g}" if initial_quantity is not None else ""
    package_menu = ft.PopupMenuButton(
        icon=ft.Icons.ARROW_DROP_DOWN,
        tooltip="Choose a recognized package",
        items=[],
        visible=bool(initial_units),
    )
    unit_field = ft.TextField(
        label="Unit / package *",
        value=(
            package_unit_name(initial_unit)
            if initial_unit is not None else evidence_amount[1] if evidence_amount else ""
        ),
        width=180,
        dense=True,
        text_size=12.5,
        suffix=package_menu,
    )
    grams_field = ft.TextField(
        label="Normalized grams preview",
        value=f"{initial_grams:.3f}" if initial_grams is not None else "",
        width=150,
        dense=True,
        text_size=12.5,
        keyboard_type=ft.KeyboardType.NUMBER,
        read_only=True,
    )
    weight_label = muted_text(resolved.label, size=11)
    grams_state = {
        "source": (
            GRAMS_SOURCE_CATALOG_ESTIMATE if initial_unit else resolved.source
            or GRAMS_SOURCE_USER_ENTERED
        ),
        "unit": initial_unit,
        "units": initial_units,
    }
    price = (
        normalize_money(facts.printed_price, positive=True)
        if facts.printed_price and facts.printed_price > 0 else None
    )
    price_field = ft.TextField(
        label="Price",
        value=f"{price:.2f}" if price else "",
        width=120,
        dense=True,
        text_size=12.5,
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    currency_field = ft.TextField(
        label="Currency",
        value=facts.printed_currency or "",
        width=110,
        dense=True,
        text_size=12.5,
    )
    destination = ft.Dropdown(
        label="Destination",
        width=230,
        text_size=12,
        options=[
            *(
                [ft.DropdownOption(key=ACTION_APPLY, text="Apply to current Plan")]
                if current_plan_live() else []
            ),
            ft.DropdownOption(key=ACTION_PANTRY, text="Add to Pantry"),
            ft.DropdownOption(key=ACTION_CUSTOM, text="Add to Custom Pantry"),
            ft.DropdownOption(key=ACTION_IGNORE, text="Not added"),
        ],
        value=(
            ACTION_APPLY if _default_apply(state, match.selected_food_id)
            else ACTION_PANTRY if match.selected_food_id else ACTION_CUSTOM
        ),
    )
    for field in (
        food_dropdown, name_field, brand_field, quantity_field, unit_field, grams_field,
        destination, price_field, currency_field,
    ):
        style_field(field)

    remaining_label = muted_text("", size=11)
    match_reason_label = muted_text("", size=11)

    def refresh_preview() -> bool:
        try:
            quantity = normalize_quantity(quantity_field.value or "")
            unit_name = _canonical_unit_name(unit_field.value)
            if not unit_name:
                raise ValueError("unit required")
            food = state.foods_by_id.get(food_dropdown.value or "")
            bound_unit: PackageUnit | None = grams_state["unit"]
            if bound_unit is not None and food is not None and unit_name in {
                _canonical_unit_name(package_unit_name(bound_unit)),
                _canonical_unit_name(bound_unit.package_label),
            }:
                grams = bound_unit.to_grams(quantity, food)
            elif food is not None:
                grams = to_grams(quantity, unit_name, food)
            else:
                grams = convert_to_grams(quantity, unit_name)
            quantity_field.error_text = None
            unit_field.error_text = None
            if grams is None or grams <= 0:
                grams_field.value = "0.000" if destination.value == ACTION_CUSTOM else ""
                if destination.value == ACTION_CUSTOM:
                    return True
                unit_field.error_text = "Choose a unit that can be converted for this food."
                return destination.value == ACTION_IGNORE
            grams_field.value = f"{grams:.3f}"
            return True
        except (ValueError, TypeError):
            if not (unit_field.value or "").strip():
                unit_field.error_text = "Unit is required."
            else:
                quantity_field.error_text = "Enter a positive quantity."
            grams_field.value = ""
            return False

    def refresh_remaining(food) -> None:
        remaining_label.value = ""
        if not current_plan_live() or state.saved_plan is None or food is None:
            return
        allocation = allocate_sources(
            state.saved_plan, state.pantry, state.foods_by_id
        ).get(food.id)
        if allocation is not None:
            remaining_label.value = (
                "Plan remaining: "
                + format_grams(food, allocation.gap, grams_state["unit"])
            )

    def on_package_change(selected: PackageUnit) -> None:
        food = state.foods_by_id.get(food_dropdown.value or "")
        if food is None:
            return
        grams_state["unit"] = selected
        grams_state["source"] = GRAMS_SOURCE_CATALOG_ESTIMATE
        unit_field.value = package_unit_name(selected)
        weight_label.value = f"Catalog estimate: {selected.package_label}"
        preview_ok = refresh_preview()
        refresh_remaining(food)
        confirm_button.disabled = not preview_ok
        page.update()

    def refresh_package_menu(units: tuple[PackageUnit, ...]) -> None:
        package_menu.items = [
            ft.PopupMenuItem(
                content=unit.package_label,
                on_click=lambda e, chosen=unit: on_package_change(chosen),
            )
            for unit in units
        ]
        package_menu.visible = bool(units)

    def on_food_change(e) -> None:
        food_dropdown.error_text = None
        food = state.foods_by_id.get(food_dropdown.value or "")
        current = resolve_weight(facts, food)
        units, selected = purchase_units(food)
        evidence = _evidence_amount_unit(facts, food, current)
        description = " ".join(filter(None, [facts.package_text, facts.observed_name]))
        if evidence is not None and not (
            selected is not None and selected.package_label in description
        ):
            selected = None
        grams_state["units"] = units
        grams_state["unit"] = selected
        grams_state["source"] = (
            GRAMS_SOURCE_CATALOG_ESTIMATE if selected else current.source
            or GRAMS_SOURCE_USER_ENTERED
        )
        refresh_package_menu(units)
        if selected is not None:
            quantity_field.value = f"{normalize_quantity(facts.quantity or 1):g}"
            unit_field.value = package_unit_name(selected)
            weight_label.value = f"Catalog estimate: {selected.package_label}"
        elif evidence is not None:
            quantity_field.value = f"{normalize_quantity(evidence[0]):g}"
            unit_field.value = evidence[1]
            weight_label.value = current.label
        else:
            quantity_field.value = f"{normalize_quantity(facts.quantity or 1):g}"
            unit_field.value = ""
            weight_label.value = "Quantity and unit require confirmation"
        candidate = next(
            (
                candidate for candidate in match.candidates
                if candidate.food_id == (food.id if food else None)
            ),
            None,
        )
        match_reason_label.value = (
            candidate.reason if candidate is not None
            else "User selected catalog food" if food is not None else ""
        )
        destination.options = [
            *(
                [ft.DropdownOption(key=ACTION_APPLY, text="Apply to current Plan")]
                if current_plan_live() else []
            ),
            ft.DropdownOption(key=ACTION_PANTRY, text="Add to Pantry"),
            ft.DropdownOption(key=ACTION_CUSTOM, text="Add to Custom Pantry"),
            ft.DropdownOption(key=ACTION_IGNORE, text="Not added"),
        ]
        destination.value = (
            ACTION_APPLY if _default_apply(state, food_dropdown.value)
            else ACTION_PANTRY if food is not None else ACTION_CUSTOM
        )
        preview_ok = refresh_preview()
        refresh_remaining(food)
        confirm_button.disabled = not preview_ok
        page.update()

    food_dropdown.on_change = on_food_change
    confirm_button = ft.TextButton(content="Confirm import")

    def on_quantity_change(e) -> None:
        confirm_button.disabled = not refresh_preview()
        page.update()

    quantity_field.on_change = on_quantity_change
    def on_unit_change(e) -> None:
        selected: PackageUnit | None = grams_state["unit"]
        if selected is not None and _canonical_unit_name(unit_field.value) not in {
            _canonical_unit_name(package_unit_name(selected)),
            _canonical_unit_name(selected.package_label),
        }:
            grams_state["unit"] = None
            grams_state["source"] = GRAMS_SOURCE_USER_ENTERED
        confirm_button.disabled = not refresh_preview()
        page.update()

    def on_destination_change(e) -> None:
        confirm_button.disabled = not refresh_preview()
        page.update()

    unit_field.on_change = on_unit_change
    destination.on_change = on_destination_change
    on_food_change(None)

    def on_confirm(e) -> None:
        current_matcher = _matcher(state)
        validation = validate_context_against_state(context_box["value"], state)
        if not validation.valid:
            if validation.close_dialog:
                page.pop_dialog()
                _show_message(page, validation.message or "This photo dialog is no longer current.")
                return
            if validation.rerun_duplicate_check:
                page.pop_dialog()
                _show_message(
                    page,
                    validation.message or "Photo import history changed; start the import again.",
                )
                return
            if validation.rerun_matcher:
                refreshed = current_matcher.match(facts, plan_food_ids=_plan_food_ids(state))
                food_dropdown.options = _candidate_options(state, refreshed)
                food_dropdown.value = refreshed.selected_food_id
            on_food_change(None)
            context_box["value"] = _context(
                state, operation_id, analysis_id, current_matcher
            )
            _show_message(page, validation.message or "Confirm the refreshed result again.")
            page.update()
            return

        action = destination.value or ACTION_IGNORE
        if action == ACTION_IGNORE:
            page.pop_dialog()
            return
        try:
            quantity = normalize_quantity(quantity_field.value or "")
            quantity_field.error_text = None
        except ValueError:
            quantity_field.error_text = "Enter a positive quantity."
            page.update()
            return
        if not refresh_preview():
            page.update()
            return
        raw_unit = (unit_field.value or "").strip()
        if not raw_unit:
            unit_field.error_text = "Unit is required."
            page.update()
            return
        try:
            grams = normalize_grams(
                grams_field.value or 0,
                positive=action != ACTION_CUSTOM,
            )
        except ValueError:
            unit_field.error_text = "Choose a unit that can be converted for this food."
            page.update()
            return
        entered_price: float | None = None
        if (price_field.value or "").strip():
            try:
                entered_price = normalize_money(price_field.value, positive=True)
                price_field.error_text = None
            except ValueError:
                price_field.error_text = "Enter a positive price or leave it blank."
                page.update()
                return
        entered_currency = (currency_field.value or "").strip().upper() or None

        purchase_inputs: list[PurchaseInput] = []
        custom_items: list[CustomPantryItem] = []
        if action == ACTION_CUSTOM:
            custom_items.append(CustomPantryItem(
                id=deterministic_custom_pantry_id(operation_id, 0, 0),
                original_name=(name_field.value or "").strip() or facts.generic_food_name,
                display_name=(name_field.value or "").strip() or facts.generic_food_name,
                amount=quantity,
                unit=raw_unit,
                grams_estimate=grams,
                brand=(brand_field.value or "").strip(),
                price=entered_price,
                created_at=datetime.now().isoformat(timespec="seconds"),
            ))
        else:
            food = state.foods_by_id.get(food_dropdown.value or "")
            if food is None:
                food_dropdown.error_text = "Choose a catalog food or use Custom Pantry."
                page.update()
                return
            if grams is None:
                grams_field.error_text = "Catalog purchases require positive grams."
                page.update()
                return
            purchase_inputs.append(PurchaseInput(
                event_id=deterministic_purchase_event_id(operation_id, 0, 0),
                food_id=food.id,
                raw_name=(name_field.value or "").strip() or facts.generic_food_name,
                brand=(brand_field.value or "").strip() or None,
                package_label=(
                    grams_state["unit"].package_label
                    if grams_state["unit"] is not None else None
                ),
                package_id=(
                    grams_state["unit"].option_for(food).package_id
                    if grams_state["unit"] is not None else None
                ),
                grams=grams,
                grams_source=grams_state["source"] or GRAMS_SOURCE_USER_ENTERED,
                quantity=quantity,
                line_total=entered_price,
                price_source=(
                    PRICE_SOURCE_VISIBLE
                    if entered_price is not None
                    and price is not None
                    and entered_price == price
                    and entered_currency == ((facts.printed_currency or "").upper() or None)
                    else PRICE_SOURCE_USER if entered_price is not None
                    else PRICE_SOURCE_UNKNOWN
                ),
                currency=entered_currency,
                apply_to_plan=action == ACTION_APPLY,
                group_id=operation_id,
                origin=ORIGIN_PRODUCT_PHOTO,
                source_line_index=0,
                segment_index=0,
            ))
        confirm_button.disabled = True
        page.update()
        saving = ImageProcessingView(page, title="Saving image import")
        saving.show(
            "Saving the product image and purchase",
            "Pantry, purchase history, Plan progress, and the image are saved together.",
        )
        try:
            purchase_unit_map = {
                item.event_id: raw_unit for item in purchase_inputs
            }
            command = PhotoImportCommand.create(
                operation_id=operation_id,
                kind=PhotoKind.PRODUCT,
                images=[analyzed.image],
                plan_id=context_box["value"].plan_id,
                context=context_box["value"],
                purchase_inputs=purchase_inputs,
                custom_items=custom_items,
                purchase_units=purchase_unit_map,
                duplicate_acknowledgement=duplicate_acknowledgement,
            )
            commit_photo_import(state, command=command, images=[analyzed.image])
        except TransactionRecoveryRequiredError:
            saving.show_failure(ImageFailureDetails(
                summary="The import requires recovery before more data can be saved.",
                stage="Transaction recovery",
                reason=(
                    "Files may already contain the import, so it was not reported as "
                    "rolled back. Writes are paused to protect the recovery journal."
                ),
                suggestions=("Restart RightMeal and review the recovered Pantry.",),
            ))
            return
        except (StalePhotoImportContext, DuplicatePhotoImport) as exc:
            saving.close()
            page.pop_dialog()
            _show_message(page, str(exc))
            page.update()
            return
        except Exception:
            confirm_button.disabled = False
            saving.show_failure(ImageFailureDetails(
                summary="The product image import could not be saved.",
                stage="Atomic import transaction",
                reason="The save failed, so all image and inventory changes were rolled back.",
                suggestions=(
                    "Check available disk space and folder permissions.",
                    "Close this page and retry the import.",
                ),
            ))
            return
        saving.close()
        page.pop_dialog()
        _call_after_commit(page, on_committed)

    confirm_button.on_click = on_confirm
    page.show_dialog(ft.AlertDialog(
        title=ft.Text(
            "Confirm product photo",
            size=15,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
        ),
        content=ft.Container(
            width=560,
            height=580,
            content=ft.Column([
                muted_text(
                    "The photo was used only to extract visible facts. Catalog matching "
                    "and match scores below were computed locally.",
                    size=12,
                ),
                ft.Image(src=analyzed.image.content, height=150, fit=ft.BoxFit.CONTAIN),
                food_dropdown,
                ft.Row([quantity_field, unit_field, grams_field], spacing=10, wrap=True),
                remaining_label,
                ft.ExpansionTile(
                    title=ft.Text("Advanced details", size=12.5),
                    expanded=False,
                    controls=[ft.Column([
                        name_field,
                        brand_field,
                        ft.Row([price_field, currency_field], spacing=8),
                        destination,
                        muted_text(
                            "Apply to current Plan logs the purchase, adds it to Pantry, "
                            "and rebuilds Plan progress. Add to Pantry leaves it off-plan. "
                            "Custom Pantry remains inert until explicitly linked.",
                            size=10.5,
                        ),
                        muted_text(
                            "Visible evidence: " + (
                                "; ".join(facts.visible_evidence) or "None"
                            ),
                            size=10.5,
                        ),
                        weight_label,
                        match_reason_label,
                        _candidate_summary(state, match),
                    ], spacing=8)],
                ),
            ], spacing=9, tight=True, scroll=ft.ScrollMode.AUTO),
        ),
        actions=[
            ft.TextButton(content="Cancel", on_click=lambda event: page.pop_dialog()),
            confirm_button,
        ],
    ))


async def run_product_photo_flow(
    page: ft.Page,
    state: AppState,
    picker: ft.FilePicker,
    on_committed: Callable[[], None],
) -> None:
    analyzer = _guarded_analyzer(page, state)
    if analyzer is None:
        return
    analysis_id = state.begin_photo_analysis()
    processing = ImageProcessingView(page, title="Processing product image")
    _show_message(
        page,
        "The selected image will be sent to OpenAI for visual extraction. Crop "
        "sensitive content first; card or member details cannot be reliably redacted.",
    )
    selected = await _pick_images(
        page,
        picker,
        "Pick a photo of the purchased food",
        allow_multiple=False,
        processing_view=processing,
    )
    if not selected:
        return
    processing.show(
        "Analyzing the product image",
        "Visible product facts are being extracted. Catalog matching stays local.",
    )
    analyzed = await analyzer.analyze_product(selected[0], "image/jpeg")
    if not state.is_current_photo_analysis(analysis_id):
        processing.close()
        return
    if analyzed is None:
        processing.show_failure(ImageFailureDetails(
            summary="The product image could not be processed.",
            stage="Image analysis",
            reason=(
                "The image could not be decoded, the analysis request failed, or "
                "the response did not contain valid product facts."
            ),
            suggestions=(
                "Use a clear JPG or PNG image.",
                "Make sure the product label is visible and well lit.",
                "Check the OpenAI key and network connection, then retry.",
            ),
        ))
        return
    duplicate = check_duplicate_import(
        PhotoKind.PRODUCT,
        [analyzed.image.sha256],
        None,
        state.photo_imports,
        purchases=state.purchase_log,
        pantry=state.pantry,
    )
    if duplicate.requires_confirmation:
        processing.close()
        async def continue_anyway(e) -> None:
            page.pop_dialog()
            acknowledged_operation_id = new_operation_id()
            acknowledgement = DuplicateAcknowledgement(
                operation_id=acknowledged_operation_id,
                previous_operation_id=duplicate.previous_operation_id or "",
                image_hash=duplicate.matched_image_hash or analyzed.image.sha256,
                ledger_revision=state.photo_import_revision,
            )
            open_product_confirm_dialog(
                page,
                state,
                analyzed,
                on_committed,
                analysis_id=analysis_id,
                duplicate_acknowledgement=acknowledgement,
                operation_id=acknowledged_operation_id,
            )
            page.update()

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("Previously imported image"),
            content=ft.Text(duplicate.message or "Continue with this image?"),
            actions=[
                ft.TextButton(content="Cancel", on_click=lambda event: page.pop_dialog()),
                ft.TextButton(content="Continue anyway", on_click=continue_anyway),
            ],
        ))
        return
    processing.close()
    open_product_confirm_dialog(
        page, state, analyzed, on_committed, analysis_id=analysis_id
    )
    page.update()


def _receipt_default_action(
    state: AppState,
    line: ReceiptLineFacts,
    match: MatchResult,
) -> str:
    if line.classification is not ReceiptLineClassification.MERCHANDISE:
        return ACTION_IGNORE
    if line.possible_duplicate:
        return ACTION_IGNORE
    if match.selected_food_id:
        return ACTION_APPLY if _default_apply(state, match.selected_food_id) else ACTION_PANTRY
    return ACTION_CUSTOM


def open_receipt_confirm_dialog(
    page: ft.Page,
    state: AppState,
    receipt: ReceiptFacts,
    images: Sequence[AnalyzedPhoto],
    on_committed: Callable[[], None],
    *,
    analysis_id: int,
    duplicate_acknowledgement: DuplicateAcknowledgement | None = None,
    duplicate_message: str | None = None,
    operation_id: str | None = None,
) -> None:
    operation_id = operation_id or new_operation_id()
    local_matcher = _matcher(state)
    context_box = {"value": _context(state, operation_id, analysis_id, local_matcher)}
    plan_live = state.saved_plan is not None and not is_historical(state.saved_plan)
    image_by_segment = {index: item.image for index, item in enumerate(images)}
    action_options = [
        *(
            [ft.DropdownOption(key=ACTION_APPLY, text="Apply to current Plan")]
            if plan_live else []
        ),
        ft.DropdownOption(key=ACTION_PANTRY, text="Add to Pantry"),
        ft.DropdownOption(key=ACTION_CUSTOM, text="Add to Custom Pantry"),
        ft.DropdownOption(key=ACTION_IGNORE, text="Not added"),
    ]

    def receipt_units(
        line: ReceiptLineFacts,
        food,
        resolved,
    ) -> tuple[tuple[PackageUnit, ...], PackageUnit | None, tuple[float, str, float] | None]:
        if food is None:
            return (), None, _evidence_amount_unit(line, None, resolved)
        explicit = tuple(package_unit(food, package) for package in resolved.package_options)
        planned = plan_package_units(food, state.saved_plan if plan_live else None)
        recent = recent_purchase_package_unit(food, state.purchase_log)
        unique = (
            package_unit(food, food.package_options[0])
            if len(food.package_options) == 1 else None
        )
        units = tuple(dict.fromkeys([
            *explicit,
            *planned,
            *((recent,) if recent is not None else ()),
            *((unique,) if unique is not None else ()),
        ]))
        evidence = _evidence_amount_unit(line, food, resolved)
        selected: PackageUnit | None = explicit[0] if len(explicit) == 1 else None
        # Any observed weight that did not identify one exact catalog package
        # outranks Plan/recent/catalog estimates.
        if selected is None and evidence is None:
            if planned:
                selected = max(planned, key=lambda value: value.grams)
            elif recent is not None:
                selected = recent
            else:
                selected = unique
        return units, selected, evidence

    rows: list[dict] = []
    body: list[ft.Control] = [
        *(
            [ft.Container(
                padding=10,
                bgcolor=theme.WARN_BG,
                border=ft.Border.all(1, theme.WARN_BORDER),
                border_radius=8,
                content=ft.Text(
                    duplicate_message or "Some items from this receipt still remain in Pantry.",
                    size=11.5,
                    color=theme.WARN_INK,
                ),
            )]
            if duplicate_acknowledgement is not None else []
        ),
        muted_text(
            "Each segment passed local coverage checks. Restore a possible duplicate "
            "only when it is a real separate purchase.",
            size=12,
        ),
        muted_text(
            f"Confirmed item spend: {confirmed_item_spend(receipt.lines):.2f} "
            f"{receipt.currency or ''}",
            size=12,
        ),
    ]
    for line in receipt.lines:
        if line.classification is not ReceiptLineClassification.MERCHANDISE:
            image = image_by_segment.get(line.segment_index)
            crop_control: ft.Control = ft.Container(width=130, height=58)
            if image is not None:
                try:
                    crop_control = ft.Image(
                        src=crop_region(image.content, line.bounding_region),
                        width=130,
                        height=58,
                        fit=ft.BoxFit.CONTAIN,
                    )
                except ValueError:
                    pass
            body.append(ft.Container(
                padding=10,
                border=ft.Border.all(1, theme.BORDER),
                border_radius=10,
                content=ft.Row([
                    crop_control,
                    ft.Column([
                        ft.Text(line.raw_printed_text or "Receipt header", size=12),
                        muted_text(
                            f"{line.classification.value} · evidence only",
                            size=10.5,
                        ),
                    ], spacing=3, expand=True),
                ], spacing=8),
            ))
            # Headers and other non-merchandise evidence never enter matching,
            # fingerprints, purchase inputs, or destination controls.
            continue
        match = local_matcher.match(line, plan_food_ids=_plan_food_ids(state))
        food_dropdown = ft.Dropdown(
            label="Local catalog match",
            width=240,
            text_size=11.5,
            editable=True,
            enable_filter=True,
            enable_search=True,
            options=_candidate_options(state, match),
            value=match.selected_food_id,
        )
        food = state.foods_by_id.get(match.selected_food_id or "")
        resolved = resolve_weight(line, food)
        initial_units, initial_package_unit, evidence = receipt_units(
            line, food, resolved
        )
        initial_amount = (
            normalize_quantity(line.quantity or 1)
            if initial_package_unit is not None
            else evidence[0] if evidence is not None else normalize_quantity(line.quantity or 1)
        )
        initial_unit_name = (
            package_unit_name(initial_package_unit)
            if initial_package_unit is not None
            else evidence[1] if evidence is not None else ""
        )
        initial_grams = (
            initial_package_unit.to_grams(initial_amount, food)
            if initial_package_unit is not None and food is not None
            else evidence[2] if evidence is not None else None
        )
        quantity_field = ft.TextField(
            label="Quantity / Amount *",
            width=115,
            dense=True,
            text_size=11.5,
            value=f"{initial_amount:g}",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        unit_field = ft.TextField(
            label="Unit *",
            width=105,
            dense=True,
            text_size=11.5,
            value=initial_unit_name,
        )
        grams_field = ft.TextField(
            label="Grams preview",
            width=100,
            dense=True,
            text_size=11.5,
            value=f"{initial_grams:.3f}" if initial_grams is not None else "",
            keyboard_type=ft.KeyboardType.NUMBER,
            read_only=True,
        )
        action = ft.Dropdown(
            label="Destination",
            width=175,
            text_size=11.5,
            options=list(action_options),
            value=_receipt_default_action(state, line, match),
        )
        for field in (food_dropdown, quantity_field, unit_field, grams_field, action):
            style_field(field)
        package_dropdown = ft.Dropdown(
            label="Catalog package estimate",
            width=190,
            text_size=11,
            options=[
                ft.DropdownOption(key=unit.package_label, text=unit.package_label)
                for unit in initial_units
            ],
            visible=bool(initial_units),
            value=(
                initial_package_unit.package_label
                if initial_package_unit is not None else None
            ),
        )
        style_field(package_dropdown)
        row = {
            "line": line,
            "match": match,
            "food": food_dropdown,
            "quantity": quantity_field,
            "unit": unit_field,
            "grams": grams_field,
            "grams_source": resolved.source,
            "action": action,
            "package": package_dropdown,
            "package_unit": initial_package_unit,
            "weight_edited": False,
        }

        def refresh_row(current=row) -> bool:
            action_value = current["action"].value or ACTION_IGNORE
            if action_value == ACTION_IGNORE:
                current["quantity"].error_text = None
                current["unit"].error_text = None
                return True
            try:
                amount = normalize_quantity(current["quantity"].value or "")
                current["quantity"].error_text = None
            except ValueError:
                current["quantity"].error_text = "Positive amount required."
                current["grams"].value = ""
                return False
            unit_name = _canonical_unit_name(current["unit"].value)
            if not unit_name:
                current["unit"].error_text = "Unit is required."
                current["grams"].value = ""
                return False
            selected_food = state.foods_by_id.get(current["food"].value or "")
            bound: PackageUnit | None = current["package_unit"]
            if bound is not None and selected_food is not None and unit_name in {
                _canonical_unit_name(package_unit_name(bound)),
                _canonical_unit_name(bound.package_label),
            }:
                grams = bound.to_grams(amount, selected_food)
                current["grams_source"] = GRAMS_SOURCE_CATALOG_ESTIMATE
            elif selected_food is not None:
                grams = to_grams(amount, unit_name, selected_food)
                current["grams_source"] = GRAMS_SOURCE_USER_ENTERED
            else:
                grams = convert_to_grams(amount, unit_name)
                current["grams_source"] = GRAMS_SOURCE_USER_ENTERED
            if grams is None or grams <= 0:
                if action_value == ACTION_CUSTOM:
                    current["unit"].error_text = None
                    current["grams"].value = "0.000"
                    return True
                current["unit"].error_text = "Unit cannot be converted for this food."
                current["grams"].value = ""
                return False
            current["unit"].error_text = None
            current["grams"].value = f"{grams:.3f}"
            return True

        def on_amount_or_unit_change(e, current=row) -> None:
            bound: PackageUnit | None = current["package_unit"]
            if bound is not None and _canonical_unit_name(current["unit"].value) not in {
                _canonical_unit_name(package_unit_name(bound)),
                _canonical_unit_name(bound.package_label),
            }:
                current["package_unit"] = None
                current["package"].value = None
            refresh_row(current)
            page.update()

        quantity_field.on_change = on_amount_or_unit_change
        unit_field.on_change = on_amount_or_unit_change
        action.on_change = lambda e, current=row: (refresh_row(current), page.update())

        def on_package_change(e, current=row) -> None:
            selected_food = state.foods_by_id.get(current["food"].value or "")
            if selected_food is None:
                return
            package = next(
                (
                    value for value in selected_food.package_options
                    if value.label == current["package"].value
                ),
                None,
            )
            if package is not None:
                current["package_unit"] = package_unit(selected_food, package)
                current["quantity"].value = f"{normalize_quantity(current['line'].quantity or 1):g}"
                current["unit"].value = package_unit_name(current["package_unit"])
                current["grams_source"] = GRAMS_SOURCE_CATALOG_ESTIMATE
                current["weight_edited"] = False
                refresh_row(current)
                page.update()

        package_dropdown.on_change = on_package_change

        def on_food_change(e, current=row) -> None:
            selected_food = state.foods_by_id.get(current["food"].value or "")
            current_weight = resolve_weight(current["line"], selected_food)
            current_units, selected_unit, current_evidence = receipt_units(
                current["line"], selected_food, current_weight
            )
            current["grams_source"] = current_weight.source
            current["weight_edited"] = False
            current["package_unit"] = selected_unit
            current["package"].options = [
                ft.DropdownOption(key=unit.package_label, text=unit.package_label)
                for unit in current_units
            ]
            if selected_food is not None and selected_unit is not None:
                current["package"].value = selected_unit.package_label
                current["quantity"].value = f"{normalize_quantity(current['line'].quantity or 1):g}"
                current["unit"].value = package_unit_name(selected_unit)
            else:
                current["package"].value = None
                if current_evidence is not None:
                    current["quantity"].value = f"{normalize_quantity(current_evidence[0]):g}"
                    current["unit"].value = current_evidence[1]
                else:
                    current["quantity"].value = f"{normalize_quantity(current['line'].quantity or 1):g}"
                    current["unit"].value = ""
            current["package"].visible = bool(current_units)
            refresh_row(current)
            page.update()

        food_dropdown.on_change = on_food_change
        row["refresh_for_food"] = on_food_change
        row["refresh_row"] = refresh_row
        rows.append(row)
        image = image_by_segment[line.segment_index]
        try:
            crop = crop_region(image.content, line.bounding_region)
            crop_control: ft.Control = ft.Image(
                src=crop, width=130, height=58, fit=ft.BoxFit.CONTAIN
            )
        except ValueError:
            crop_control = ft.Container(width=130, height=58)
        duplicate_text = (
            f" · {line.duplicate_reason}" if line.possible_duplicate else ""
        )
        price = confirmed_line_total(line)
        body.append(ft.Container(
            padding=10,
            border=ft.Border.all(1, theme.BORDER),
            border_radius=10,
            content=ft.Column([
                ft.Row([
                    crop_control,
                    ft.Column([
                        ft.Text(line.raw_printed_text or "Unnamed line", size=12),
                        muted_text(
                            f"{line.classification.value}{duplicate_text}", size=10.5
                        ),
                        muted_text(
                            f"Printed line total: {price:.2f} {receipt.currency or ''}"
                            if price else "No eligible printed item total",
                            size=10.5,
                        ),
                    ], spacing=3, expand=True),
                ], spacing=8),
                _candidate_summary(state, match, limit=2),
                ft.Row(
                    [food_dropdown, quantity_field, unit_field, grams_field, action],
                    spacing=7,
                    wrap=True,
                ),
                package_dropdown,
                muted_text(resolved.label, size=10.5),
            ], spacing=5),
        ))

    skip_unknown = ft.Checkbox(
        label="Skip all items with unknown weight",
        value=False,
        active_color=theme.PRIMARY,
    )
    body.append(skip_unknown)
    confirm_button = ft.TextButton(content=(
        "Add anyway"
        if duplicate_acknowledgement is not None else "Confirm receipt import"
    ))

    def on_confirm(e) -> None:
        current_matcher = _matcher(state)
        validation = validate_context_against_state(context_box["value"], state)
        if not validation.valid:
            if validation.close_dialog:
                page.pop_dialog()
                _show_message(page, validation.message or "This photo dialog is no longer current.")
                return
            if validation.rerun_duplicate_check:
                page.pop_dialog()
                _show_message(
                    page,
                    validation.message or "Photo import history changed; start the import again.",
                )
                return
            for row in rows:
                refreshed = current_matcher.match(
                    row["line"], plan_food_ids=_plan_food_ids(state)
                )
                row["food"].options = _candidate_options(state, refreshed)
                row["food"].value = refreshed.selected_food_id
                row["action"].value = _receipt_default_action(
                    state, row["line"], refreshed
                )
                row["refresh_for_food"](None)
            context_box["value"] = _context(
                state, operation_id, analysis_id, current_matcher
            )
            _show_message(page, validation.message or "Confirm the refreshed result again.")
            page.update()
            return

        purchase_inputs: list[PurchaseInput] = []
        custom_items: list[CustomPantryItem] = []
        for row in rows:
            line: ReceiptLineFacts = row["line"]
            action = row["action"].value or ACTION_IGNORE
            if action == ACTION_IGNORE:
                continue
            if not row["refresh_row"](row):
                if skip_unknown.value and action != ACTION_CUSTOM:
                    continue
                page.update()
                return
            try:
                amount = normalize_quantity(row["quantity"].value or "")
            except ValueError:
                row["quantity"].error_text = "Positive amount required."
                page.update()
                return
            raw_unit = (row["unit"].value or "").strip()
            if not raw_unit:
                row["unit"].error_text = "Unit is required."
                page.update()
                return
            try:
                grams = normalize_grams(
                    row["grams"].value or 0,
                    positive=action != ACTION_CUSTOM,
                )
            except ValueError:
                if skip_unknown.value and action != ACTION_CUSTOM:
                    continue
                row["unit"].error_text = "Unit cannot be converted for this food."
                page.update()
                return
            if action == ACTION_CUSTOM:
                custom_items.append(CustomPantryItem(
                    id=deterministic_custom_pantry_id(
                        operation_id, line.segment_index, line.source_line_index
                    ),
                    original_name=line.raw_printed_text or line.generic_item_name,
                    display_name=line.generic_item_name or line.raw_printed_text,
                    amount=amount,
                    unit=raw_unit,
                    grams_estimate=grams,
                    brand=line.brand or "",
                    price=confirmed_line_total(line),
                    created_at=datetime.now().isoformat(timespec="seconds"),
                ))
                continue
            food = state.foods_by_id.get(row["food"].value or "")
            if food is None:
                row["food"].error_text = "Choose a catalog food or use Custom Pantry."
                page.update()
                return
            total = confirmed_line_total(line)
            purchase_inputs.append(PurchaseInput(
                event_id=deterministic_purchase_event_id(
                    operation_id, line.segment_index, line.source_line_index
                ),
                food_id=food.id,
                raw_name=line.raw_printed_text or line.generic_item_name,
                brand=line.brand,
                package_label=(
                    row["package_unit"].package_label
                    if row["package_unit"] is not None else None
                ),
                package_id=(
                    row["package_unit"].option_for(food).package_id
                    if row["package_unit"] is not None else None
                ),
                grams=grams,
                grams_source=row["grams_source"] or GRAMS_SOURCE_USER_ENTERED,
                quantity=amount,
                line_total=total,
                currency=(receipt.currency or "").strip().upper() or None,
                price_source=PRICE_SOURCE_RECEIPT if total else PRICE_SOURCE_UNKNOWN,
                store=receipt.store_name or "",
                apply_to_plan=action == ACTION_APPLY,
                group_id=operation_id,
                origin=ORIGIN_RECEIPT,
                source_line_index=line.source_line_index,
                segment_index=line.segment_index,
            ))
        if not purchase_inputs and not custom_items:
            _show_message(page, "Nothing is selected from this receipt.")
            return
        confirm_button.disabled = True
        page.update()
        saving = ImageProcessingView(page, title="Saving image import")
        saving.show(
            "Saving receipt images and confirmed items",
            "Pantry, purchase history, Plan progress, and receipt images are saved together.",
        )
        try:
            unit_by_event = {
                deterministic_purchase_event_id(
                    operation_id,
                    row["line"].segment_index,
                    row["line"].source_line_index,
                ): (row["unit"].value or "").strip()
                for row in rows
                if (row["action"].value or ACTION_IGNORE) not in (
                    ACTION_IGNORE, ACTION_CUSTOM
                )
            }
            command = PhotoImportCommand.create(
                operation_id=operation_id,
                kind=PhotoKind.RECEIPT,
                images=[item.image for item in images],
                plan_id=context_box["value"].plan_id,
                context=context_box["value"],
                purchase_inputs=purchase_inputs,
                custom_items=custom_items,
                purchase_units={
                    item.event_id: unit_by_event[item.event_id]
                    for item in purchase_inputs
                },
                transaction_fingerprint=receipt_transaction_fingerprint(receipt),
                duplicate_acknowledgement=duplicate_acknowledgement,
            )
            commit_photo_import(
                state,
                command=command,
                images=[item.image for item in images],
            )
        except TransactionRecoveryRequiredError:
            saving.show_failure(ImageFailureDetails(
                summary="The import requires recovery before more data can be saved.",
                stage="Transaction recovery",
                reason=(
                    "Files may already contain the import, so it was not reported as "
                    "rolled back. Writes are paused to protect the recovery journal."
                ),
                suggestions=("Restart RightMeal and review the recovered Pantry.",),
            ))
            return
        except (StalePhotoImportContext, DuplicatePhotoImport) as exc:
            saving.close()
            page.pop_dialog()
            _show_message(page, str(exc))
            page.update()
            return
        except Exception:
            confirm_button.disabled = False
            saving.show_failure(ImageFailureDetails(
                summary="The receipt image import could not be saved.",
                stage="Atomic import transaction",
                reason="The save failed, so all image and inventory changes were rolled back.",
                suggestions=(
                    "Check available disk space and folder permissions.",
                    "Close this page and retry the import.",
                ),
            ))
            return
        saving.close()
        page.pop_dialog()
        _call_after_commit(page, on_committed)

    confirm_button.on_click = on_confirm
    page.show_dialog(ft.AlertDialog(
        title=ft.Text(
            f"Confirm receipt{' · ' + receipt.store_name if receipt.store_name else ''}",
            size=15,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
        ),
        content=ft.Container(
            width=700,
            height=590,
            content=ft.Column(body, spacing=9, tight=True, scroll=ft.ScrollMode.AUTO),
        ),
        actions=[
            ft.TextButton(content="Cancel", on_click=lambda event: page.pop_dialog()),
            confirm_button,
        ],
    ))


def _open_receipt_item_review(
    page: ft.Page,
    receipt: ReceiptFacts,
    images: Sequence[AnalyzedPhoto],
    on_reviewed: Callable[[], None],
) -> None:
    """Require an explicit check for every logical item after weak conflicts."""

    items = tuple(line for line in receipt.logical_items if not line.possible_duplicate)
    checks = [
        ft.Checkbox(
            label=(
                line.raw_printed_text
                or f"Merchandise item {index + 1}"
            ),
            value=False,
        )
        for index, line in enumerate(items)
    ]
    confirm = ft.TextButton(content="Confirm every item", disabled=True)

    def update_confirm(e=None) -> None:
        confirm.disabled = not checks or not all(bool(check.value) for check in checks)
        page.update()

    for check in checks:
        check.on_change = update_confirm

    def accept(e) -> None:
        if confirm.disabled:
            return
        page.pop_dialog()
        on_reviewed()

    confirm.on_click = accept
    quantity_total = sum(line.quantity or 1 for line in items)
    crop_previews: list[ft.Control] = []
    for index, analyzed in enumerate(images):
        segment = analyzed.receipt
        if segment is None:
            continue
        try:
            preview = crop_region(analyzed.image.content, segment.merchandise_area)
        except ValueError:
            preview = analyzed.image.content
        crop_previews.extend([
            muted_text(f"Merchandise crop · segment {index + 1}", size=11),
            ft.Image(src=preview, height=150, fit=ft.BoxFit.CONTAIN),
        ])

    printed = (
        str(receipt.printed_item_count)
        if receipt.printed_item_count is not None else "not visible"
    )
    body = ft.Column([
        muted_text(
            "A weak count or image-evidence signal remains. Review the complete "
            "merchandise crop and check every logical item before continuing.",
            size=12,
        ),
        ft.Text(
            f"Logical items: {len(items)} · quantity total: {quantity_total:g} · "
            f"printed item count: {printed}",
            size=12,
        ),
        *crop_previews,
        ft.Divider(height=1, color=theme.BORDER),
        *checks,
    ], spacing=8, tight=True, scroll=ft.ScrollMode.AUTO)
    page.show_dialog(ft.AlertDialog(
        modal=True,
        title=ft.Text("Review every receipt item"),
        content=ft.Container(width=620, height=600, content=body),
        actions=[
            ft.TextButton(content="Cancel import", on_click=lambda e: page.pop_dialog()),
            confirm,
        ],
    ))


def _open_manual_receipt_boundary_review(
    page: ft.Page,
    analyzed: list[AnalyzedPhoto],
    on_confirmed: Callable[[], None],
) -> None:
    """At five segments, require the user to select the final item and line."""

    last = analyzed[-1]
    receipt = last.receipt
    if receipt is None:
        return
    items = tuple(
        sorted(
            (line for line in receipt.logical_items if not line.possible_duplicate),
            key=lambda line: (line.bounding_region.y1, line.source_line_index),
        )
    )
    if not items:
        _show_message(page, "No logical merchandise item is available for manual review.")
        return

    payment_barriers = (
        tuple(
            barrier for barrier in receipt.layout_evidence.barriers
            if barrier.kind in (
                ReceiptBarrierKind.PAYMENT_TENDER,
                ReceiptBarrierKind.TRANSACTION,
            )
        )
        if receipt.layout_evidence is not None else ()
    )
    barrier_limit = min(
        (barrier.bounding_region.y1 for barrier in payment_barriers),
        default=0.999,
    )
    maximum_percent = max(1.0, min(99.9, barrier_limit * 100.0 - 0.1))
    selection = ft.Dropdown(
        label="Last logical merchandise item *",
        width=560,
        options=[
            ft.DropdownOption(
                key=str(line.source_line_index),
                text=(line.raw_printed_text or f"Item {index + 1}")[:90],
            )
            for index, line in enumerate(items)
        ],
        value=None,
    )
    style_field(selection)
    boundary = ft.Slider(
        min=1,
        max=maximum_percent,
        value=min(90.0, maximum_percent),
        label="{value}%",
        disabled=True,
    )
    boundary_label = muted_text("Choose the final item, then place the boundary line.", size=12)
    error = ft.Text("", size=12, color=theme.DANGER, visible=False)
    confirm = ft.TextButton(content="Confirm manual boundary", disabled=True)
    touched = {"item": False, "boundary": False}

    def selected_item() -> ReceiptLineFacts | None:
        try:
            source_index = int(selection.value or "")
        except ValueError:
            return None
        return next(
            (line for line in items if line.source_line_index == source_index),
            None,
        )

    def update_button() -> None:
        confirm.disabled = not (touched["item"] and touched["boundary"])

    def on_item_change(e) -> None:
        item = selected_item()
        touched["item"] = item is not None
        touched["boundary"] = False
        error.visible = False
        if item is None:
            boundary.disabled = True
        else:
            minimum = (item.bounding_region.y2 + 0.002) * 100.0
            boundary.min = min(minimum, maximum_percent)
            boundary.max = maximum_percent
            boundary.value = (boundary.min + boundary.max) / 2.0
            boundary.disabled = boundary.min >= boundary.max
            if boundary.disabled:
                error.value = (
                    "No safe boundary exists after this item and before the detected "
                    "payment or transaction area."
                )
                error.visible = True
            boundary_label.value = (
                f"Boundary line: {boundary.value:.1f}% down the final image. "
                "Move it explicitly to confirm."
            )
        update_button()
        page.update()

    def on_boundary_change(e) -> None:
        touched["boundary"] = True
        boundary_label.value = (
            f"Boundary line: {float(boundary.value):.1f}% down the final image."
        )
        error.visible = False
        update_button()
        page.update()

    def accept(e) -> None:
        item = selected_item()
        if item is None or not touched["boundary"]:
            return
        boundary_y = float(boundary.value) / 100.0
        try:
            confirmed = confirm_manual_boundary(
                receipt,
                last_item_source_index=item.source_line_index,
                boundary_y=boundary_y,
            )
            cropped_image = crop_to_boundary(last.image, boundary_y)
            rebased = rebase_receipt_to_boundary_crop(confirmed, boundary_y)
            coverage = validate_receipt_coverage(
                rebased,
                cropped_image.width,
                cropped_image.height,
            )
            if coverage.strong_conflict or (
                not coverage.complete and not coverage.manual_review_required
            ):
                raise ValueError(" ".join(coverage.reasons) or "The boundary is not safe.")
        except ValueError as exc:
            error.value = str(exc)
            error.visible = True
            page.update()
            return

        diagnostics = last.diagnostics
        if diagnostics is not None:
            diagnostics = replace(
                diagnostics,
                image_width=cropped_image.width,
                image_height=cropped_image.height,
                failure_stage=coverage.failure_stage,
            )
        analyzed[-1] = replace(
            last,
            analysis=replace(last.analysis, receipt=rebased),
            image=cropped_image,
            coverage=coverage,
            diagnostics=diagnostics,
            failure_message=None,
        )
        page.pop_dialog()
        on_confirmed()

    selection.on_change = on_item_change
    boundary.on_change = on_boundary_change
    confirm.on_click = accept
    page.show_dialog(ft.AlertDialog(
        modal=True,
        title=ft.Text("Set a manual merchandise boundary"),
        content=ft.Container(
            width=650,
            height=650,
            content=ft.Column([
                muted_text(
                    "Five segments still have no reliable automatic end boundary. "
                    "Review the full final image, explicitly choose its last merchandise "
                    "item, and place a line before payment or transaction evidence.",
                    size=12,
                ),
                ft.Image(src=last.image.content, height=390, fit=ft.BoxFit.CONTAIN),
                selection,
                boundary,
                boundary_label,
                error,
            ], spacing=9, tight=True, scroll=ft.ScrollMode.AUTO),
        ),
        actions=[
            ft.TextButton(content="Cancel import", on_click=lambda e: page.pop_dialog()),
            confirm,
        ],
    ))


async def _run_legacy_receipt_flow(
    page: ft.Page,
    state: AppState,
    picker: ft.FilePicker,
    on_committed: Callable[[], None],
) -> None:
    analyzer = _guarded_analyzer(page, state)
    if analyzer is None:
        return
    analysis_id = state.begin_photo_analysis()
    processing = ImageProcessingView(page, title="Processing receipt images")
    _show_message(
        page,
        "Receipt images will be sent to OpenAI for visual extraction. Crop names, "
        "addresses, card/member details, and QR codes before selection.",
    )
    pending = await _pick_images(
        page,
        picker,
        "Pick one receipt photo or 2-5 overlapping segment photos in top-to-bottom order",
        allow_multiple=True,
        processing_view=processing,
    )
    if not pending:
        return
    analyzed: list[AnalyzedPhoto] = []

    def finish_session() -> None:
        segments = [value.receipt for value in analyzed if value.receipt is not None]
        if len(segments) != len(analyzed):
            processing.show_failure(ImageFailureDetails(
                summary="The selected images could not form a receipt session.",
                stage="Receipt session assembly",
                reason="One or more analyzed segments did not contain receipt facts.",
                suggestions=("Restart the import with ordered receipt images.",),
            ))
            return
        if receipt_session_status(segments) is not ReceiptSessionStatus.AUTO_CONFIRMABLE:
            processing.show_failure(ImageFailureDetails(
                summary="The receipt import is incomplete.",
                stage="Receipt session validation",
                reason="No reliable automatic or user-confirmed merchandise boundary exists.",
                suggestions=("Restart the import and explicitly review the final boundary.",),
            ))
            return
        receipt = combine_receipt_segments(segments)
        fingerprint = receipt_transaction_fingerprint(receipt)
        duplicate = check_duplicate_import(
            PhotoKind.RECEIPT,
            [value.image.sha256 for value in analyzed],
            fingerprint,
            state.photo_imports,
            purchases=state.purchase_log,
            pantry=state.pantry,
        )
        if duplicate.blocked:
            processing.close()
            _show_message(page, duplicate.message or "This receipt was already imported.")
            return

        acknowledged_operation_id = (
            new_operation_id() if duplicate.requires_confirmation else None
        )
        acknowledgement = (
            DuplicateAcknowledgement(
                operation_id=acknowledged_operation_id or "",
                previous_operation_id=duplicate.previous_operation_id or "",
                image_hash=duplicate.matched_image_hash,
                ledger_revision=state.photo_import_revision,
            )
            if acknowledged_operation_id is not None else None
        )

        def open_confirmation() -> None:
            open_receipt_confirm_dialog(
                page,
                state,
                receipt,
                analyzed,
                on_committed,
                analysis_id=analysis_id,
                duplicate_acknowledgement=acknowledgement,
                duplicate_message=duplicate.message,
                operation_id=acknowledged_operation_id,
            )
            page.update()

        processing.close()
        if any(
            value.coverage is not None and value.coverage.manual_review_required
            for value in analyzed
        ):
            _open_receipt_item_review(page, receipt, analyzed, open_confirmation)
        else:
            open_confirmation()

    while pending:
        if len(analyzed) + len(pending) > 5:
            processing.show_failure(ImageFailureDetails(
                summary="The receipt session contains too many images.",
                stage="Receipt session validation",
                reason="A receipt import supports no more than five ordered segments.",
                suggestions=("Restart and select one to five receipt images.",),
            ))
            return
        first_index = len(analyzed) + 1
        last_index = len(analyzed) + len(pending)
        segment_label = (
            f"segment {first_index}" if first_index == last_index
            else f"segments {first_index}–{last_index}"
        )
        processing.show(
            f"Analyzing receipt {segment_label}",
            "Each image is checked independently for readable and complete merchandise lines.",
        )
        analyzed_values = await asyncio.gather(*(
            analyzer.analyze_receipt(image, "image/jpeg") for image in pending
        ))
        if not state.is_current_photo_analysis(analysis_id):
            processing.close()
            return
        if any(value is None for value in analyzed_values):
            failed = [
                str(first_index + index)
                for index, value in enumerate(analyzed_values)
                if value is None
            ]
            processing.show_failure(ImageFailureDetails(
                summary="At least one receipt image could not be processed.",
                stage="Image analysis",
                reason=f"Unreadable or invalid segment(s): {', '.join(failed)}.",
                suggestions=(
                    "Use clear, upright JPG or PNG images.",
                    "Keep printed merchandise lines sharp and fully visible.",
                    "Check the OpenAI key and network connection, then retry.",
                ),
            ))
            return
        batch = [value for value in analyzed_values if value is not None]
        coordinate_failure = next(
            (value.failure_message for value in batch if value.failure_message),
            None,
        )
        if coordinate_failure:
            # Strong conflicts expose one generic message only. Detailed regions,
            # indexes, and retry counts remain in privacy-safe diagnostics.
            processing.close()
            _show_message(page, coordinate_failure)
            return
        incomplete = [
            reason
            for value in batch
            for reason in (value.coverage.reasons if value.coverage else ())
            if value.coverage is not None
            and not value.coverage.manual_review_required
            and "missing_end_boundary" not in value.coverage.conflict_codes
        ]
        if incomplete:
            failed_value = next(
                value for value in batch
                if value.coverage is not None and not value.coverage.complete
            )
            processing.show_failure(ImageFailureDetails(
                summary="Receipt confirmation is blocked because coverage checks failed.",
                stage=(
                    failed_value.coverage.failure_stage.replace("_", " ").title()
                    if failed_value.coverage and failed_value.coverage.failure_stage
                    else "Receipt coverage validation"
                ),
                reason=" ".join(dict.fromkeys(incomplete)),
                suggestions=(
                    "Retake the image with all visible merchandise lines in frame.",
                    "Use additional overlapping segments for long receipts.",
                    "Make sure the image is sharp and high resolution.",
                ),
                diagnostics=_receipt_failure_diagnostics(failed_value),
            ))
            return
        batch_segments = [value.receipt for value in batch if value.receipt is not None]
        if len(batch_segments) != len(batch):
            processing.show_failure(ImageFailureDetails(
                summary="The selected image was not recognized as a receipt.",
                stage="Content classification",
                reason="The analysis did not return receipt facts for every image.",
                suggestions=("Select only grocery receipt images and retry.",),
            ))
            return
        analyzed.extend(batch)
        session_segments = [
            value.receipt for value in analyzed if value.receipt is not None
        ]
        status = receipt_session_status(session_segments)
        if status is ReceiptSessionStatus.AUTO_CONFIRMABLE:
            break
        if status is ReceiptSessionStatus.MANUAL_REVIEW_REQUIRED:
            processing.close()
            _open_manual_receipt_boundary_review(page, analyzed, finish_session)
            return
        if status is ReceiptSessionStatus.BLOCKED:
            processing.show_failure(ImageFailureDetails(
                summary="The receipt import is blocked.",
                stage="Receipt session validation",
                reason="The ordered receipt segments could not form a safe session.",
                suggestions=("Restart with one to five ordered receipt images.",),
            ))
            return
        processing.close()
        _show_message(
            page,
            "No reliable merchandise end boundary is visible. Choose the next segment, "
            "or cancel the picker to cancel the import.",
        )
        pending = await _pick_images(
            page,
            picker,
            "Pick the next receipt segment(s) in top-to-bottom order",
            allow_multiple=True,
            processing_view=processing,
        )
        if not pending:
            return

    finish_session()


# -- Coordinate-free receipt import -----------------------------------------


@dataclass(frozen=True)
class _ReceiptDecision:
    item: ReceiptScanItem
    destination: str
    display_name: str
    food_id: str | None
    amount: float
    unit: str
    grams: float
    grams_source: str
    package_unit: PackageUnit | None = None


def _scan_item_name(item: ReceiptScanItem) -> str:
    return (item.generic_item_name or item.raw_printed_text or "Unnamed receipt item").strip()


def _live_plan_food_ids(state: AppState) -> set[str]:
    if state.saved_plan is None or is_historical(state.saved_plan):
        return set()
    return {item.food_id for item in state.saved_plan.basket}


def _scan_item_total(item: ReceiptScanItem) -> float | None:
    return confirmed_line_total(item)


def _automatic_receipt_decision(
    state: AppState,
    item: ReceiptScanItem,
    matcher: CatalogMatcher,
) -> tuple[_ReceiptDecision | None, str | None]:
    """Resolve only facts that Add by photo already considers safe defaults."""

    if item.kind is not ReceiptScanItemKind.FOOD:
        return None, "The line was not confidently classified as food."
    if item.possible_duplicate:
        return None, item.duplicate_reason or "Possible overlapping receipt item."
    match = matcher.match(item, plan_food_ids=_live_plan_food_ids(state))
    food = state.foods_by_id.get(match.selected_food_id or "")
    if food is None:
        return None, "No high-confidence local catalog match was found."
    resolved = resolve_weight(item, food)
    evidence = _evidence_amount_unit(item, food, resolved)
    if evidence is None:
        return None, "The amount or weight could not be resolved safely."
    amount, unit, grams = evidence
    bound_package = None
    if resolved.source == GRAMS_SOURCE_CATALOG_ESTIMATE and len(resolved.package_options) == 1:
        bound_package = package_unit(food, resolved.package_options[0])
    destination = (
        ACTION_APPLY if food.id in _live_plan_food_ids(state) else ACTION_CUSTOM
    )
    return _ReceiptDecision(
        item=item,
        destination=destination,
        display_name=_scan_item_name(item),
        food_id=food.id,
        amount=normalize_quantity(amount),
        unit=unit,
        grams=normalize_grams(grams, positive=True),
        grams_source=resolved.source or GRAMS_SOURCE_USER_ENTERED,
        package_unit=bound_package,
    ), None


def _show_receipt_failure(
    processing: ImageProcessingView,
    *,
    summary: str,
    stage: str,
    reason: str,
    suggestions: Sequence[str] = (),
    diagnostics: Sequence[tuple[str, str]] = (),
) -> None:
    processing.show_failure(ImageFailureDetails(
        summary=summary,
        stage=stage,
        reason=reason,
        suggestions=tuple(suggestions),
        diagnostics=tuple(diagnostics),
    ))


def _show_receipt_report(
    page: ft.Page,
    pantry_items: Sequence[str],
    custom_items: Sequence[str],
    not_added: Sequence[str],
) -> None:
    def section(title: str, icon: str, color: str, values: Sequence[str]) -> ft.Control:
        controls: list[ft.Control] = [
            ft.Row([
                ft.Icon(icon, size=19, color=color),
                ft.Text(
                    f"{title} ({len(values)})",
                    size=13,
                    weight=ft.FontWeight.W_600,
                    color=theme.TEXT,
                ),
            ], spacing=7),
        ]
        controls.extend(
            ft.Row([
                ft.Icon(ft.Icons.CIRCLE, size=6, color=color),
                ft.Text(value, size=12, color=theme.TEXT, expand=True),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.START)
            for value in values
        )
        if not values:
            controls.append(muted_text("None", size=11.5))
        return ft.Column(controls, spacing=6, tight=True)

    page.show_dialog(ft.AlertDialog(
        modal=True,
        title=ft.Text(
            "Receipt import report",
            size=16,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
        ),
        content=ft.Container(
            width=560,
            height=470,
            content=ft.Column([
                muted_text(
                    "Every detected line is listed once below. Plan foods were added "
                    "to My Pantry and applied to the current Plan; other confirmed "
                    "foods were saved in Custom items.",
                    size=12,
                ),
                section(
                    "Added to My Pantry",
                    ft.Icons.KITCHEN_OUTLINED,
                    theme.PRIMARY,
                    pantry_items,
                ),
                ft.Divider(height=1, color=theme.BORDER),
                section(
                    "Added to Custom items",
                    ft.Icons.INVENTORY_2_OUTLINED,
                    theme.WARN_INK,
                    custom_items,
                ),
                ft.Divider(height=1, color=theme.BORDER),
                section(
                    "Not added",
                    ft.Icons.BLOCK_OUTLINED,
                    theme.TEXT_MUTED,
                    not_added,
                ),
            ], spacing=12, scroll=ft.ScrollMode.AUTO),
        ),
        actions=[ft.TextButton(content="Close", on_click=lambda event: page.pop_dialog())],
    ))


def _open_receipt_item_resolution_dialog(
    page: ft.Page,
    state: AppState,
    item: ReceiptScanItem,
    matcher: CatalogMatcher,
    *,
    item_number: int,
    item_total: int,
    review_reason: str,
    on_resolved: Callable[[_ReceiptDecision | None, str | None], None],
) -> None:
    """Ask only for the ambiguous facts; matching/weight rules stay shared."""

    plan_ids = _live_plan_food_ids(state)
    match = matcher.match(item, plan_food_ids=plan_ids)
    food_dropdown = ft.Dropdown(
        label="Catalog match (optional for Custom items)",
        width=420,
        editable=True,
        enable_filter=True,
        enable_search=True,
        menu_height=300,
        text_size=12,
        options=_candidate_options(state, match),
        value=match.selected_food_id,
    )
    name_field = ft.TextField(
        label="Food name *",
        value=_scan_item_name(item),
        dense=True,
        text_size=12,
    )
    amount_field = ft.TextField(
        label="Quantity / amount *",
        width=145,
        dense=True,
        text_size=12,
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    unit_field = ft.TextField(
        label="Unit *",
        width=130,
        dense=True,
        text_size=12,
    )
    grams_field = ft.TextField(
        label="Grams preview",
        width=130,
        dense=True,
        read_only=True,
        text_size=12,
    )
    package_dropdown = ft.Dropdown(
        label="Recognized package",
        width=300,
        text_size=11.5,
        visible=False,
    )
    destination_label = ft.Text("", size=12, color=theme.TEXT_MUTED)
    error_label = ft.Text("", size=11.5, color=theme.DANGER, visible=False)
    add_button = ft.TextButton(content="Add item")
    unit_state: dict[str, PackageUnit | None] = {"bound": None}
    for control in (
        food_dropdown, name_field, amount_field, unit_field, grams_field,
        package_dropdown,
    ):
        style_field(control)

    def available_units(food) -> tuple[tuple[PackageUnit, ...], PackageUnit | None]:
        if food is None:
            return (), None
        resolved = resolve_weight(item, food)
        explicit = tuple(package_unit(food, package) for package in resolved.package_options)
        planned = plan_package_units(
            food, state.saved_plan if food.id in plan_ids else None
        )
        recent = recent_purchase_package_unit(food, state.purchase_log)
        unique = (
            package_unit(food, food.package_options[0])
            if len(food.package_options) == 1 else None
        )
        units = tuple(dict.fromkeys([
            *explicit,
            *planned,
            *((recent,) if recent is not None else ()),
            *((unique,) if unique is not None else ()),
        ]))
        selected = explicit[0] if len(explicit) == 1 else (
            max(planned, key=lambda value: value.grams) if planned else recent or unique
        )
        return units, selected

    def refresh(*, reset_values: bool = False) -> bool:
        food = state.foods_by_id.get(food_dropdown.value or "")
        destination = ACTION_APPLY if food is not None and food.id in plan_ids else ACTION_CUSTOM
        destination_label.value = (
            "Destination: My Pantry + current Plan"
            if destination == ACTION_APPLY else "Destination: Custom items"
        )
        if reset_values:
            resolved = resolve_weight(item, food)
            evidence = _evidence_amount_unit(item, food, resolved)
            units, selected = available_units(food)
            package_dropdown.options = [
                ft.DropdownOption(key=unit.package_label, text=unit.package_label)
                for unit in units
            ]
            package_dropdown.visible = bool(units)
            unit_state["bound"] = None
            if evidence is not None:
                amount_field.value = f"{normalize_quantity(evidence[0]):g}"
                unit_field.value = evidence[1]
                package_dropdown.value = None
            elif selected is not None:
                unit_state["bound"] = selected
                package_dropdown.value = selected.package_label
                amount_field.value = f"{normalize_quantity(item.quantity or 1):g}"
                unit_field.value = package_unit_name(selected)
            else:
                package_dropdown.value = None
                amount_field.value = f"{normalize_quantity(item.quantity or 1):g}"
                unit_field.value = "item"
        try:
            amount = normalize_quantity(amount_field.value or "")
            amount_field.error_text = None
        except ValueError:
            amount_field.error_text = "Enter a positive amount."
            grams_field.value = ""
            add_button.disabled = True
            return False
        unit = _canonical_unit_name(unit_field.value)
        if not unit:
            unit_field.error_text = "Unit is required."
            grams_field.value = ""
            add_button.disabled = True
            return False
        bound = unit_state["bound"]
        grams = None
        if food is not None and bound is not None and unit in {
            _canonical_unit_name(bound.package_label),
            _canonical_unit_name(package_unit_name(bound)),
        }:
            grams = bound.to_grams(amount, food)
        elif food is not None:
            grams = to_grams(amount, unit, food)
        else:
            grams = convert_to_grams(amount, unit)
        if destination == ACTION_APPLY and (grams is None or grams <= 0):
            unit_field.error_text = "Choose a package or unit that converts to grams."
            grams_field.value = ""
            add_button.disabled = True
            return False
        unit_field.error_text = None
        grams_field.value = f"{grams:.3f}" if grams is not None else "0.000"
        add_button.disabled = not bool((name_field.value or "").strip())
        return not add_button.disabled

    def on_food_change(event) -> None:
        del event
        refresh(reset_values=True)
        page.update()

    def on_value_change(event) -> None:
        del event
        bound = unit_state["bound"]
        if bound is not None and _canonical_unit_name(unit_field.value) not in {
            _canonical_unit_name(bound.package_label),
            _canonical_unit_name(package_unit_name(bound)),
        }:
            unit_state["bound"] = None
            package_dropdown.value = None
        refresh()
        page.update()

    def on_package_change(event) -> None:
        del event
        food = state.foods_by_id.get(food_dropdown.value or "")
        if food is None:
            return
        selected = next(
            (unit for unit in available_units(food)[0] if unit.package_label == package_dropdown.value),
            None,
        )
        unit_state["bound"] = selected
        if selected is not None:
            amount_field.value = f"{normalize_quantity(item.quantity or 1):g}"
            unit_field.value = package_unit_name(selected)
        refresh()
        page.update()

    def skip(event) -> None:
        del event
        page.pop_dialog()
        on_resolved(None, f"{_scan_item_name(item)} — not added after review")

    def accept(event) -> None:
        del event
        if not refresh():
            page.update()
            return
        food = state.foods_by_id.get(food_dropdown.value or "")
        destination = ACTION_APPLY if food is not None and food.id in plan_ids else ACTION_CUSTOM
        amount = normalize_quantity(amount_field.value or "")
        unit = (unit_field.value or "").strip()
        grams = normalize_grams(grams_field.value or 0, positive=destination == ACTION_APPLY)
        bound = unit_state["bound"]
        decision = _ReceiptDecision(
            item=item,
            destination=destination,
            display_name=(name_field.value or "").strip(),
            food_id=food.id if food is not None else None,
            amount=amount,
            unit=unit,
            grams=grams,
            grams_source=(
                GRAMS_SOURCE_CATALOG_ESTIMATE
                if bound is not None else GRAMS_SOURCE_USER_ENTERED
            ),
            package_unit=bound,
        )
        page.pop_dialog()
        on_resolved(decision, None)

    food_dropdown.on_change = on_food_change
    amount_field.on_change = on_value_change
    unit_field.on_change = on_value_change
    package_dropdown.on_change = on_package_change
    name_field.on_change = lambda event: (refresh(), page.update())
    add_button.on_click = accept
    refresh(reset_values=True)

    page.show_dialog(ft.AlertDialog(
        modal=True,
        title=ft.Text(
            f"Review receipt item {item_number} of {item_total}",
            size=15,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
        ),
        content=ft.Container(
            width=560,
            content=ft.Column([
                ft.Container(
                    padding=10,
                    bgcolor=theme.SURFACE_TINT,
                    border_radius=8,
                    content=ft.Column([
                        ft.Text(item.raw_printed_text or _scan_item_name(item), size=12.5),
                        muted_text(review_reason, size=11),
                    ], spacing=4, tight=True),
                ),
                name_field,
                food_dropdown,
                ft.Row([amount_field, unit_field, grams_field], spacing=8, wrap=True),
                package_dropdown,
                destination_label,
                error_label,
            ], spacing=10, tight=True, scroll=ft.ScrollMode.AUTO),
        ),
        actions=[
            ft.TextButton(content="Do not add", on_click=skip),
            add_button,
        ],
    ))


def _receipt_item_key(item: ReceiptScanItem) -> tuple[int, int]:
    return item.segment_index, item.source_item_index


def _receipt_decision_status(decision: _ReceiptDecision) -> str:
    amount = f"{decision.amount:g} {decision.unit}".strip()
    if decision.destination == ACTION_APPLY:
        return f"{amount} · My Pantry + current Plan"
    return f"{amount} · Custom items"


def _open_receipt_batch_review_dialog(
    page: ft.Page,
    state: AppState,
    receipt: ReceiptScanFacts,
    matcher: CatalogMatcher,
    *,
    initial_decisions: Sequence[_ReceiptDecision],
    review_reasons: dict[tuple[int, int], str],
    ignored_reasons: dict[ReceiptScanItemKind, str],
    on_confirmed: Callable[[list[_ReceiptDecision], list[str]], None],
    duplicate_message: str | None = None,
) -> None:
    """Review every receipt line together; open the editor only on demand."""

    decisions = {
        _receipt_item_key(decision.item): decision for decision in initial_decisions
    }
    checks: dict[tuple[int, int], ft.Checkbox] = {}
    statuses: dict[tuple[int, int], ft.Text] = {}
    names: dict[tuple[int, int], ft.Text] = {}
    explicit_skips: dict[tuple[int, int], str] = {}
    rows: list[ft.Control] = []
    editable_items = tuple(
        item for item in receipt.items
        if item.kind not in {ReceiptScanItemKind.DISCOUNT, ReceiptScanItemKind.SUMMARY}
    )
    edit_positions = {
        _receipt_item_key(item): index + 1
        for index, item in enumerate(editable_items)
    }
    selected_summary = ft.Text(size=11.5, color=theme.TEXT_MUTED)

    def update_selected_summary(event=None) -> None:
        del event
        ready = sum(
            1 for key, check in checks.items()
            if key in decisions and bool(check.value)
        )
        available = sum(1 for key in checks if key in decisions)
        selected_summary.value = (
            f"{ready} item{'s' if ready != 1 else ''} selected for import"
            + (f" · {available} ready" if available != ready else "")
        )
        page.update()

    def default_not_added_reason(item: ReceiptScanItem) -> str:
        key = _receipt_item_key(item)
        if key in explicit_skips:
            return explicit_skips[key]
        if item.kind in ignored_reasons:
            return f"{_scan_item_name(item)} — {ignored_reasons[item.kind]}"
        reason = review_reasons.get(key)
        if reason:
            return f"{_scan_item_name(item)} — not confirmed: {reason}"
        return f"{_scan_item_name(item)} — not confirmed on receipt review"

    def make_edit_handler(item: ReceiptScanItem) -> Callable[[object], None]:
        key = _receipt_item_key(item)

        def edit(event) -> None:
            del event
            current = decisions.get(key)
            reason = review_reasons.get(key)
            if current is not None:
                reason = "Change the match, amount, unit, or destination for this item."
            elif item.kind in ignored_reasons:
                reason = f"Detected as {ignored_reasons[item.kind]}; edit only if this is food."

            def resolved(
                decision: _ReceiptDecision | None,
                ignored: str | None,
            ) -> None:
                check = checks[key]
                status = statuses[key]
                if decision is not None:
                    decisions[key] = decision
                    explicit_skips.pop(key, None)
                    check.disabled = False
                    check.value = True
                    names[key].value = decision.display_name
                    status.value = _receipt_decision_status(decision)
                    status.color = theme.PRIMARY_DARK
                else:
                    decisions.pop(key, None)
                    check.disabled = True
                    check.value = False
                    if ignored:
                        explicit_skips[key] = ignored
                    status.value = "Not added · edit this item to include it"
                    status.color = theme.TEXT_MUTED
                update_selected_summary()

            _open_receipt_item_resolution_dialog(
                page,
                state,
                item,
                matcher,
                item_number=edit_positions[key],
                item_total=len(editable_items),
                review_reason=reason or "Confirm the item details before importing it.",
                on_resolved=resolved,
            )

        return edit

    for item in receipt.items:
        key = _receipt_item_key(item)
        decision = decisions.get(key)
        permanently_ignored = item.kind in {
            ReceiptScanItemKind.DISCOUNT,
            ReceiptScanItemKind.SUMMARY,
        }
        check = ft.Checkbox(
            label="Confirm",
            value=decision is not None,
            disabled=decision is None,
            active_color=theme.PRIMARY,
            on_change=update_selected_summary,
        )
        if decision is not None:
            status_value = _receipt_decision_status(decision)
            status_color = theme.PRIMARY_DARK
        elif item.kind in ignored_reasons:
            status_value = f"Not added · {ignored_reasons[item.kind]}"
            status_color = theme.TEXT_MUTED
        else:
            status_value = f"Needs review · {review_reasons.get(key, 'details are uncertain')}"
            status_color = theme.WARN_INK
        status = ft.Text(status_value, size=11, color=status_color)
        name = ft.Text(
            decision.display_name if decision is not None else _scan_item_name(item),
            size=12.5,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
        )
        checks[key] = check
        statuses[key] = status
        names[key] = name
        controls: list[ft.Control] = [
            ft.Column([
                name,
                muted_text(item.raw_printed_text or _scan_item_name(item), size=10.5),
                status,
            ], spacing=3, expand=True),
            check,
        ]
        if not permanently_ignored:
            controls.append(ft.TextButton(
                content="Edit",
                icon=ft.Icons.EDIT_OUTLINED,
                on_click=make_edit_handler(item),
            ))
        rows.append(ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=9),
            border=ft.Border.all(1, theme.BORDER),
            border_radius=9,
            bgcolor=theme.SURFACE,
            content=ft.Row(
                controls,
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        ))

    def confirm(event) -> None:
        del event
        selected: list[_ReceiptDecision] = []
        not_added: list[str] = []
        for item in receipt.items:
            key = _receipt_item_key(item)
            decision = decisions.get(key)
            if decision is not None and bool(checks[key].value):
                selected.append(decision)
            else:
                not_added.append(default_not_added_reason(item))
        page.pop_dialog()
        on_confirmed(selected, not_added)

    update_selected_summary()
    page.show_dialog(ft.AlertDialog(
        modal=True,
        title=ft.Text(
            "Review receipt items",
            size=16,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
        ),
        content=ft.Container(
            width=720,
            height=560,
            content=ft.Column([
                *(
                    [ft.Container(
                        padding=10,
                        bgcolor=theme.WARN_BG,
                        border=ft.Border.all(1, theme.WARN_BORDER),
                        border_radius=8,
                        content=ft.Text(
                            duplicate_message,
                            size=11.5,
                            color=theme.WARN_INK,
                        ),
                    )]
                    if duplicate_message else []
                ),
                muted_text(
                    "All detected lines are shown once. Ready items are already checked. "
                    "Uncheck anything you do not want, or use Edit only on an item that needs changes.",
                    size=12,
                ),
                selected_summary,
                ft.Column(rows, spacing=8, scroll=ft.ScrollMode.AUTO, expand=True),
            ], spacing=10),
        ),
        actions=[
            ft.TextButton(content="Cancel", on_click=lambda event: page.pop_dialog()),
            ft.TextButton(
                content=("Add anyway" if duplicate_message else "Import confirmed items"),
                on_click=confirm,
            ),
        ],
    ))


async def run_receipt_flow(
    page: ft.Page,
    state: AppState,
    picker: ft.FilePicker,
    on_committed: Callable[[], None],
) -> None:
    """Scan receipt items and review all detected lines on one page before saving."""

    processing = ImageProcessingView(page, title="Processing receipt")
    if state.purchase_log_error or state.photo_import_error:
        _show_receipt_failure(
            processing,
            summary="Receipt imports are paused to protect existing data.",
            stage="Purchase history loading",
            reason=state.purchase_log_error or state.photo_import_error or "Import history is unavailable.",
            suggestions=("Restart RightMeal and resolve the data-loading error first.",),
        )
        return
    analyzer = get_photo_analyzer(state.profile, state.http_client)
    if analyzer is None:
        _show_receipt_failure(
            processing,
            summary="Receipt analysis is not configured.",
            stage="OpenAI configuration",
            reason="No OpenAI API key is available to the application.",
            suggestions=("Add an OpenAI key in Profile, then retry.",),
        )
        return

    analysis_id = state.begin_photo_analysis()
    _show_message(
        page,
        "Receipt images will be sent to OpenAI for item extraction. Crop names, "
        "addresses, card/member details, and QR codes before selection.",
    )
    selected = await _pick_images(
        page,
        picker,
        "Pick one receipt image or up to five ordered overlapping images",
        allow_multiple=True,
        processing_view=processing,
    )
    if not selected:
        return
    processing.show(
        "Scanning purchased receipt items",
        "Food classification uses one coordinate-free analysis per selected image.",
    )
    outcomes = await asyncio.gather(
        *(analyzer.scan_receipt(image, "image/jpeg") for image in selected),
        return_exceptions=True,
    )
    if not state.is_current_photo_analysis(analysis_id):
        processing.close()
        return
    failure = next((value for value in outcomes if isinstance(value, Exception)), None)
    if failure is not None:
        if isinstance(failure, ReceiptScanError):
            _show_receipt_failure(
                processing,
                summary="The receipt could not be scanned safely.",
                stage=failure.stage,
                reason=failure.reason,
                suggestions=failure.suggestions,
                diagnostics=failure.diagnostics,
            )
        else:
            _show_receipt_failure(
                processing,
                summary="The receipt scan failed unexpectedly.",
                stage="Receipt analysis",
                reason=str(failure),
                suggestions=("Close this popup and retry with clear receipt images.",),
            )
        return
    analyzed = [
        value for value in outcomes if isinstance(value, AnalyzedReceiptSegment)
    ]
    try:
        receipt = combine_receipt_scans([value.receipt for value in analyzed])
    except ValueError as exc:
        _show_receipt_failure(
            processing,
            summary="The selected images could not form one receipt.",
            stage="Receipt assembly",
            reason=str(exc),
            suggestions=("Select one to five images in top-to-bottom order.",),
        )
        return

    fingerprint = receipt_transaction_fingerprint(receipt)
    duplicate = check_duplicate_import(
        PhotoKind.RECEIPT,
        [value.image.sha256 for value in analyzed],
        fingerprint,
        state.photo_imports,
        purchases=state.purchase_log,
        pantry=state.pantry,
    )
    if duplicate.blocked:
        _show_receipt_failure(
            processing,
            summary="This receipt was not imported again.",
            stage="Duplicate receipt check",
            reason=duplicate.message or "The same receipt is already in import history.",
            suggestions=("Review the existing Pantry items instead of importing it twice.",),
        )
        return

    matcher = _matcher(state)
    initial_decisions: list[_ReceiptDecision] = []
    review_reasons: dict[tuple[int, int], str] = {}
    ignored_reasons = {
        ReceiptScanItemKind.NON_FOOD: "detected as non-food",
        ReceiptScanItemKind.DISCOUNT: "discount/coupon line",
        ReceiptScanItemKind.SUMMARY: "receipt subtotal/total line",
    }
    for item in receipt.items:
        if item.kind in ignored_reasons:
            continue
        decision, reason = _automatic_receipt_decision(state, item, matcher)
        if decision is not None:
            initial_decisions.append(decision)
        else:
            review_reasons[_receipt_item_key(item)] = (
                reason or "This item needs confirmation."
            )
    processing.close()

    operation_id = new_operation_id()
    context = _context(state, operation_id, analysis_id, matcher)
    duplicate_acknowledgement = (
        DuplicateAcknowledgement(
            operation_id=operation_id,
            previous_operation_id=duplicate.previous_operation_id or "",
            image_hash=duplicate.matched_image_hash,
            ledger_revision=state.photo_import_revision,
        )
        if duplicate.requires_confirmation else None
    )

    def finish_import(
        decisions: list[_ReceiptDecision],
        not_added: list[str],
    ) -> None:
        purchase_inputs: list[PurchaseInput] = []
        custom_payloads: list[CustomPantryItem] = []
        purchase_units: dict[str, str] = {}
        pantry_report: list[str] = []
        custom_report: list[str] = []
        for decision in decisions:
            item = decision.item
            if decision.destination == ACTION_APPLY:
                if decision.food_id is None:
                    not_added.append(f"{decision.display_name} — catalog match was lost")
                    continue
                event_id = deterministic_purchase_event_id(
                    operation_id, item.segment_index, item.source_item_index
                )
                food = state.foods_by_id[decision.food_id]
                bound = decision.package_unit
                purchase_inputs.append(PurchaseInput(
                    event_id=event_id,
                    food_id=food.id,
                    raw_name=item.raw_printed_text or decision.display_name,
                    brand=item.brand,
                    package_label=bound.package_label if bound is not None else None,
                    package_id=(bound.option_for(food).package_id if bound is not None else None),
                    grams=decision.grams,
                    grams_source=decision.grams_source,
                    quantity=decision.amount,
                    line_total=_scan_item_total(item),
                    currency=(receipt.currency or "").strip().upper() or None,
                    price_source=(
                        PRICE_SOURCE_RECEIPT
                        if _scan_item_total(item) is not None else PRICE_SOURCE_UNKNOWN
                    ),
                    store=receipt.store_name or "",
                    apply_to_plan=True,
                    group_id=operation_id,
                    origin=ORIGIN_RECEIPT,
                    source_line_index=item.source_item_index,
                    segment_index=item.segment_index,
                ))
                purchase_units[event_id] = decision.unit
                pantry_report.append(
                    f"{food.name} — {decision.grams:g} g; current Plan updated"
                )
            elif decision.destination == ACTION_CUSTOM:
                custom_payloads.append(CustomPantryItem(
                    id=deterministic_custom_pantry_id(
                        operation_id, item.segment_index, item.source_item_index
                    ),
                    original_name=item.raw_printed_text or decision.display_name,
                    display_name=decision.display_name,
                    amount=decision.amount,
                    unit=decision.unit,
                    grams_estimate=decision.grams,
                    brand=item.brand or "",
                    price=_scan_item_total(item),
                    created_at=datetime.now().isoformat(timespec="seconds"),
                ))
                custom_report.append(
                    f"{decision.display_name} — {decision.amount:g} {decision.unit}"
                )

        if not purchase_inputs and not custom_payloads:
            _show_receipt_report(page, pantry_report, custom_report, not_added)
            return
        saving = ImageProcessingView(page, title="Saving receipt import")
        saving.show(
            "Saving the scanned receipt items",
            "Pantry, current Plan progress, Custom items, purchase history, and images are saved together.",
        )
        try:
            command = PhotoImportCommand.create(
                operation_id=operation_id,
                kind=PhotoKind.RECEIPT,
                images=[value.image for value in analyzed],
                plan_id=context.plan_id,
                context=context,
                purchase_inputs=purchase_inputs,
                custom_items=custom_payloads,
                purchase_units=purchase_units,
                transaction_fingerprint=fingerprint,
                duplicate_acknowledgement=duplicate_acknowledgement,
            )
            commit_photo_import(
                state,
                command=command,
                images=[value.image for value in analyzed],
            )
        except TransactionRecoveryRequiredError:
            _show_receipt_failure(
                saving,
                summary="The receipt import needs transaction recovery.",
                stage="Transaction recovery",
                reason="The save result is ambiguous, so further writes are paused.",
                suggestions=("Restart RightMeal and review the recovered Pantry.",),
            )
            return
        except (StalePhotoImportContext, DuplicatePhotoImport) as exc:
            _show_receipt_failure(
                saving,
                summary="The receipt was not saved.",
                stage="Import consistency check",
                reason=str(exc),
                suggestions=("Close the popup and scan the receipt again.",),
            )
            return
        except Exception as exc:
            _show_receipt_failure(
                saving,
                summary="The receipt items could not be saved.",
                stage="Atomic import transaction",
                reason=f"All receipt changes were rolled back: {exc}",
                suggestions=(
                    "Check available disk space and folder permissions.",
                    "Close the popup and retry the receipt import.",
                ),
            )
            return
        saving.close()
        _call_after_commit(page, on_committed)
        _show_receipt_report(page, pantry_report, custom_report, not_added)

    _open_receipt_batch_review_dialog(
        page,
        state,
        receipt,
        matcher,
        initial_decisions=initial_decisions,
        review_reasons=review_reasons,
        ignored_reasons=ignored_reasons,
        on_confirmed=finish_import,
        duplicate_message=(
            duplicate.message if duplicate.requires_confirmation else None
        ),
    )
