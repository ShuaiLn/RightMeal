"""Shared evidence-only product-photo and receipt import flows."""

from __future__ import annotations

import asyncio
import copy
import weakref
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

import flet as ft

import theme
from models.pantry import CustomPantryItem
from models.photo_analysis import (
    PhotoKind,
    ProductFacts,
    ReceiptFacts,
    ReceiptLineClassification,
    ReceiptLineFacts,
)
from models.purchase_log import (
    ORIGIN_PRODUCT_PHOTO,
    ORIGIN_RECEIPT,
    PRICE_SOURCE_RECEIPT,
    PRICE_SOURCE_UNKNOWN,
    PRICE_SOURCE_VISIBLE,
    PurchaseInput,
)
from services.pantry_matcher import CatalogMatcher, MatchResult
from services.photo_analyzer import AnalyzedPhoto, get_photo_analyzer
from services.photo_images import crop_region
from services.photo_imports import (
    InconsistentPhotoOperation,
    PhotoDialogContext,
    check_duplicate_import,
    commit_photo_import,
    deterministic_custom_pantry_id,
    deterministic_purchase_event_id,
    new_operation_id,
    receipt_transaction_fingerprint,
    validate_dialog_context,
)
from services.photo_resolution import (
    GRAMS_SOURCE_CATALOG_ESTIMATE,
    GRAMS_SOURCE_USER_ENTERED,
    confirmed_item_spend,
    confirmed_line_total,
    resolve_weight,
)
from services.receipt_validation import combine_receipt_segments
from services.source_allocation import allocate_sources, is_historical
from ui.components import food_avatar, muted_text, style_field
from ui.state import AppState

ACTION_APPLY = "apply"
ACTION_PANTRY = "pantry"
ACTION_CUSTOM = "custom"
ACTION_IGNORE = "ignore"


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
) -> list[bytes] | None:
    files = await picker.pick_files(
        dialog_title=title,
        allowed_extensions=["jpg", "jpeg", "png"],
        allow_multiple=allow_multiple,
    )
    if not files:
        return None
    if len(files) > 3:
        _show_message(page, "Choose no more than three overlapping receipt photos.")
        return None
    selected: list[bytes] = []
    for file in files:
        if not file.path:
            return None
        try:
            selected.append(Path(file.path).read_bytes())
        except OSError:
            _show_message(page, "One of the selected files could not be read.")
            return None
    return selected


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
    return PhotoDialogContext(
        operation_id=operation_id,
        analysis_id=analysis_id,
        plan_id=state.saved_plan.plan_id if state.saved_plan else None,
        catalog_signature=matcher.signature,
    )


def _call_after_commit(
    page: ft.Page,
    on_committed: Callable[[], None],
) -> None:
    try:
        on_committed()
    except Exception:
        _show_message(page, "The data was saved, but the screen could not be refreshed.")


def open_product_confirm_dialog(
    page: ft.Page,
    state: AppState,
    analyzed: AnalyzedPhoto,
    on_committed: Callable[[], None],
    *,
    analysis_id: int,
) -> None:
    facts = analyzed.product
    if facts is None:
        return
    operation_id = new_operation_id()
    local_matcher = _matcher(state)
    match = local_matcher.match(facts, plan_food_ids=_plan_food_ids(state))
    context_box = {"value": _context(state, operation_id, analysis_id, local_matcher)}
    plan_live = state.saved_plan is not None and not is_historical(state.saved_plan)

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
        label="Quantity",
        value=str(facts.quantity or 1),
        width=110,
        dense=True,
        text_size=12.5,
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    initial_food = state.foods_by_id.get(match.selected_food_id or "")
    resolved = resolve_weight(facts, initial_food)
    grams_field = ft.TextField(
        label="Total grams",
        value=f"{resolved.grams:g}" if resolved.grams else "",
        width=150,
        dense=True,
        text_size=12.5,
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    weight_label = muted_text(resolved.label, size=11)
    package_dropdown = ft.Dropdown(
        label="Catalog package estimate",
        width=260,
        text_size=11.5,
        options=[
            ft.DropdownOption(key=package.label, text=package.label)
            for package in resolved.package_options
        ],
        visible=len(resolved.package_options) > 1,
    )
    grams_state = {"source": resolved.source, "edited": False}
    price = facts.printed_price if facts.printed_price and facts.printed_price > 0 else None
    price_label = muted_text(
        f"Visible printed price: {price:.2f} {facts.printed_currency or ''}" if price else
        "No clearly visible positive item price was found.",
        size=11,
    )
    destination = ft.Dropdown(
        label="Destination",
        width=230,
        text_size=12,
        options=[
            *(
                [ft.DropdownOption(key=ACTION_APPLY, text="Apply to current Plan")]
                if plan_live else []
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
        food_dropdown, name_field, brand_field, quantity_field, grams_field,
        package_dropdown, destination,
    ):
        style_field(field)

    def on_grams_change(e) -> None:
        grams_state["source"] = GRAMS_SOURCE_USER_ENTERED
        grams_state["edited"] = True

    grams_field.on_change = on_grams_change

    def on_package_change(e) -> None:
        food = state.foods_by_id.get(food_dropdown.value or "")
        if food is None:
            return
        package = next(
            (value for value in food.package_options if value.label == package_dropdown.value),
            None,
        )
        if package is not None:
            grams_field.value = f"{package.grams:g}"
            grams_state["source"] = GRAMS_SOURCE_CATALOG_ESTIMATE
            grams_state["edited"] = False
            weight_label.value = f"Catalog estimate: {package.label}"
            page.update()

    package_dropdown.on_change = on_package_change

    def on_food_change(e) -> None:
        food_dropdown.error_text = None
        if not grams_state["edited"]:
            current = resolve_weight(facts, state.foods_by_id.get(food_dropdown.value or ""))
            grams_field.value = f"{current.grams:g}" if current.grams else ""
            grams_state["source"] = current.source
            weight_label.value = current.label
            package_dropdown.options = [
                ft.DropdownOption(key=package.label, text=package.label)
                for package in current.package_options
            ]
            package_dropdown.value = None
            package_dropdown.visible = len(current.package_options) > 1
        destination.value = (
            ACTION_APPLY if _default_apply(state, food_dropdown.value) else ACTION_PANTRY
        )
        page.update()

    food_dropdown.on_change = on_food_change
    confirm_button = ft.TextButton(content="Confirm import")

    def on_confirm(e) -> None:
        current_matcher = _matcher(state)
        validation = validate_dialog_context(
            context_box["value"],
            current_analysis_id=state.photo_analysis_seq,
            current_plan_id=state.saved_plan.plan_id if state.saved_plan else None,
            current_catalog_signature=current_matcher.signature,
        )
        if not validation.valid:
            if validation.rerun_matcher:
                refreshed = current_matcher.match(facts, plan_food_ids=_plan_food_ids(state))
                food_dropdown.options = _candidate_options(state, refreshed)
                food_dropdown.value = refreshed.selected_food_id
            destination.value = (
                ACTION_APPLY if _default_apply(state, food_dropdown.value)
                else ACTION_PANTRY if food_dropdown.value else ACTION_CUSTOM
            )
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
            quantity = int(quantity_field.value or "1")
            if quantity <= 0:
                raise ValueError
            quantity_field.error_text = None
        except ValueError:
            quantity_field.error_text = "Enter a positive whole number."
            page.update()
            return
        grams: float | None = None
        if (grams_field.value or "").strip():
            try:
                grams = float(grams_field.value)
                if grams <= 0:
                    raise ValueError
                grams_field.error_text = None
            except ValueError:
                grams_field.error_text = "Enter positive grams or leave this Custom item unknown."
                page.update()
                return

        purchase_inputs: list[PurchaseInput] = []
        custom_items: list[CustomPantryItem] = []
        if action == ACTION_CUSTOM:
            custom_items.append(CustomPantryItem(
                id=deterministic_custom_pantry_id(operation_id, 0, 0),
                original_name=(name_field.value or "").strip() or facts.generic_food_name,
                display_name=(name_field.value or "").strip() or facts.generic_food_name,
                amount=float(quantity),
                unit="item" if quantity == 1 else "items",
                grams_estimate=grams or 0.0,
                brand=(brand_field.value or "").strip(),
                price=price,
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
                package_label=facts.package_text,
                grams=grams,
                grams_source=grams_state["source"] or GRAMS_SOURCE_USER_ENTERED,
                quantity=quantity,
                line_total=price,
                price_source=PRICE_SOURCE_VISIBLE if price else PRICE_SOURCE_UNKNOWN,
                apply_to_plan=action == ACTION_APPLY,
                group_id=operation_id,
                origin=ORIGIN_PRODUCT_PHOTO,
                source_line_index=0,
                segment_index=0,
            ))
        confirm_button.disabled = True
        page.update()
        try:
            commit_photo_import(
                state,
                operation_id=operation_id,
                kind=PhotoKind.PRODUCT,
                images=[analyzed.image],
                purchase_inputs=purchase_inputs,
                custom_items=custom_items,
            )
        except (Exception, InconsistentPhotoOperation):
            confirm_button.disabled = False
            _show_message(page, "The photo import could not be saved; nothing changed.")
            page.update()
            return
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
                _candidate_summary(state, match),
                food_dropdown,
                name_field,
                brand_field,
                ft.Row([quantity_field, grams_field], spacing=10),
                weight_label,
                package_dropdown,
                price_label,
                destination,
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
    _show_message(
        page,
        "The selected image will be sent to OpenAI for visual extraction. Crop "
        "sensitive content first; card or member details cannot be reliably redacted.",
    )
    selected = await _pick_images(
        page, picker, "Pick a photo of the purchased food", allow_multiple=False
    )
    if not selected:
        return
    analyzed = await analyzer.analyze_product(selected[0], "image/jpeg")
    if analyzed is None or not state.is_current_photo_analysis(analysis_id):
        _show_message(page, "That product photo could not be read.")
        return
    duplicate = check_duplicate_import(
        PhotoKind.PRODUCT,
        [analyzed.image.sha256],
        None,
        state.photo_imports,
    )
    if duplicate.requires_confirmation:
        async def continue_anyway(e) -> None:
            page.pop_dialog()
            open_product_confirm_dialog(
                page, state, analyzed, on_committed, analysis_id=analysis_id
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
) -> None:
    operation_id = new_operation_id()
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
    rows: list[dict] = []
    body: list[ft.Control] = [
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
        grams_field = ft.TextField(
            label="Grams",
            width=100,
            dense=True,
            text_size=11.5,
            value=f"{resolved.grams:g}" if resolved.grams else "",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        action = ft.Dropdown(
            label="Destination",
            width=175,
            text_size=11.5,
            options=list(action_options),
            value=_receipt_default_action(state, line, match),
        )
        for field in (food_dropdown, grams_field, action):
            style_field(field)
        package_dropdown = ft.Dropdown(
            label="Catalog package estimate",
            width=190,
            text_size=11,
            options=[
                ft.DropdownOption(key=package.label, text=package.label)
                for package in resolved.package_options
            ],
            visible=len(resolved.package_options) > 1,
        )
        style_field(package_dropdown)
        row = {
            "line": line,
            "match": match,
            "food": food_dropdown,
            "grams": grams_field,
            "grams_source": resolved.source,
            "action": action,
            "package": package_dropdown,
            "weight_edited": False,
        }

        def on_grams_change(e, current=row) -> None:
            current["grams_source"] = GRAMS_SOURCE_USER_ENTERED
            current["weight_edited"] = True

        grams_field.on_change = on_grams_change

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
                current["grams"].value = f"{package.grams:g}"
                current["grams_source"] = GRAMS_SOURCE_CATALOG_ESTIMATE
                current["weight_edited"] = False
                page.update()

        package_dropdown.on_change = on_package_change

        def on_food_change(e, current=row) -> None:
            if not current["weight_edited"]:
                current_weight = resolve_weight(
                    current["line"],
                    state.foods_by_id.get(current["food"].value or ""),
                )
                current["grams"].value = (
                    f"{current_weight.grams:g}" if current_weight.grams else ""
                )
                current["grams_source"] = current_weight.source
                current["package"].options = [
                    ft.DropdownOption(key=package.label, text=package.label)
                    for package in current_weight.package_options
                ]
                current["package"].value = None
                current["package"].visible = len(current_weight.package_options) > 1
            page.update()

        food_dropdown.on_change = on_food_change
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
                ft.Row([food_dropdown, grams_field, action], spacing=7, wrap=True),
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
    confirm_button = ft.TextButton(content="Confirm receipt import")

    def on_confirm(e) -> None:
        current_matcher = _matcher(state)
        validation = validate_dialog_context(
            context_box["value"],
            current_analysis_id=state.photo_analysis_seq,
            current_plan_id=state.saved_plan.plan_id if state.saved_plan else None,
            current_catalog_signature=current_matcher.signature,
        )
        if not validation.valid:
            for row in rows:
                refreshed = current_matcher.match(
                    row["line"], plan_food_ids=_plan_food_ids(state)
                )
                row["food"].options = _candidate_options(state, refreshed)
                row["food"].value = refreshed.selected_food_id
                row["action"].value = _receipt_default_action(
                    state, row["line"], refreshed
                )
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
            grams: float | None = None
            if (row["grams"].value or "").strip():
                try:
                    grams = float(row["grams"].value)
                    if grams <= 0:
                        raise ValueError
                    row["grams"].error_text = None
                except ValueError:
                    row["grams"].error_text = "Positive grams?"
                    page.update()
                    return
            if action == ACTION_CUSTOM:
                custom_items.append(CustomPantryItem(
                    id=deterministic_custom_pantry_id(
                        operation_id, line.segment_index, line.source_line_index
                    ),
                    original_name=line.raw_printed_text or line.generic_item_name,
                    display_name=line.generic_item_name or line.raw_printed_text,
                    amount=float(line.quantity or 1),
                    unit="item" if (line.quantity or 1) == 1 else "items",
                    grams_estimate=grams or 0.0,
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
            if grams is None:
                if skip_unknown.value:
                    continue
                row["grams"].error_text = "Catalog purchases require grams."
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
                grams=grams,
                grams_source=row["grams_source"] or GRAMS_SOURCE_USER_ENTERED,
                quantity=line.quantity or 1,
                line_total=total,
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
        try:
            commit_photo_import(
                state,
                operation_id=operation_id,
                kind=PhotoKind.RECEIPT,
                images=[item.image for item in images],
                purchase_inputs=purchase_inputs,
                custom_items=custom_items,
                transaction_fingerprint=receipt_transaction_fingerprint(receipt),
            )
        except Exception:
            confirm_button.disabled = False
            _show_message(page, "The receipt import could not be saved; nothing changed.")
            page.update()
            return
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


async def run_receipt_flow(
    page: ft.Page,
    state: AppState,
    picker: ft.FilePicker,
    on_committed: Callable[[], None],
) -> None:
    analyzer = _guarded_analyzer(page, state)
    if analyzer is None:
        return
    analysis_id = state.begin_photo_analysis()
    _show_message(
        page,
        "Receipt images will be sent to OpenAI for visual extraction. Crop names, "
        "addresses, card/member details, and QR codes before selection.",
    )
    selected = await _pick_images(
        page,
        picker,
        "Pick one receipt photo or 2-3 overlapping segment photos",
        allow_multiple=True,
    )
    if not selected:
        return
    analyzed_values = await asyncio.gather(*(
        analyzer.analyze_receipt(image, "image/jpeg") for image in selected
    ))
    if not state.is_current_photo_analysis(analysis_id):
        return
    if any(value is None for value in analyzed_values):
        _show_message(page, "At least one receipt segment could not be read.")
        return
    analyzed = [value for value in analyzed_values if value is not None]
    incomplete = [
        reason
        for value in analyzed
        for reason in (value.coverage.reasons if value.coverage else ())
    ]
    if incomplete:
        _show_message(page, "Receipt confirmation is blocked: " + " ".join(incomplete))
        return
    segments = [value.receipt for value in analyzed if value.receipt is not None]
    if len(segments) != len(analyzed):
        _show_message(page, "The selected image is not a readable receipt.")
        return
    receipt = combine_receipt_segments(segments)
    fingerprint = receipt_transaction_fingerprint(receipt)
    duplicate = check_duplicate_import(
        PhotoKind.RECEIPT,
        [value.image.sha256 for value in analyzed],
        fingerprint,
        state.photo_imports,
    )
    if duplicate.blocked:
        _show_message(page, duplicate.message or "This receipt was already imported.")
        return
    open_receipt_confirm_dialog(
        page,
        state,
        receipt,
        analyzed,
        on_committed,
        analysis_id=analysis_id,
    )
    page.update()
