"""Pantry page: raw ingredients as a photo card grid, plus prepared meals.

Two strictly separate inventories live here:
- Raw foods (grams) — arrive by checking "Purchased" on the Plan tab or by
  adding a catalog food manually. The next plan uses this stock first.
- Prepared meals (servings) — cooked leftovers recorded from meal cards.
  They are ready meals, never raw stock: eating or discarding them touches
  only their own servings, and records reserved by the current plan are
  locked until that plan changes.
"""

from __future__ import annotations

import copy
import os
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Sequence

import flet as ft

import theme
from models import Food
from models.pantry import CUSTOM_ID_PREFIX, MAPPING_PENDING, CustomPantryItem, Pantry
from models.prepared_leftover import (
    EPSILON,
    ORIGIN_BATCH,
    STATUS_AVAILABLE,
    STATUS_DISCARDED,
    PreparedLeftover,
    component_summary_label,
    refresh_derived_fields,
)
from services.ingredient_matching import MatchCandidate, match_pantry_input
from services.meal_tracking_flow import reserved_slot_for
from services.package_units import (
    base_unit_name,
    display_amount,
    display_amount_to_grams,
    format_grams,
    package_unit_name,
    preferred_package_unit,
)
from models.quantities import normalize_grams
from services.photo_images import normalize_image
from services.photo_import_store import IMPORTED_IMAGES_DIRNAME
from services.wikimedia_images import WikimediaImageSearch
from ui.components import (
    food_photo,
    muted_text,
    pill,
    primary_button,
    section_card,
    style_field,
)
from ui.image_processing import ImageFailureDetails, ImageProcessingView
from ui.meals_section import carryover_amount_label
from ui.photo_purchase import ensure_file_picker, run_product_photo_flow, run_receipt_flow
from ui.state import AppState


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold().strip()


def resolve_food(
    typed: str,
    selected_key: str | None,
    available: Sequence[Food],
    foods_by_id: dict[str, Food],
) -> Food | None:
    """Resolve the add-box input to a catalog food. Identity is always the
    food id: the dropdown selection wins only while the visible text still
    matches it, and typed text only counts as an exact (normalized) name.
    Ambiguous partial matches ("Milk" vs "Soy Milk") never auto-pick."""
    normalized = _normalize(typed)
    selected = foods_by_id.get(selected_key or "")
    if selected is not None and (not normalized or normalized == _normalize(selected.name)):
        return selected
    matches = [food for food in available if _normalize(food.name) == normalized]
    return matches[0] if len(matches) == 1 else None


def sorted_catalog_foods(
    foods: Sequence[Food], pantry_items: dict[str, float] | None = None
) -> list[Food]:
    """All catalog foods for matching/selection — deliberately NOT filtered by
    current pantry membership (that filtering is exactly the bug this fixes).
    When pantry_items is given, already-stocked foods sort after not-yet-stocked
    ones, so the common case (adding something new) stays fast to scan."""
    items = pantry_items or {}
    return sorted(foods, key=lambda f: (f.id in items, f.name))


class AddAction(Enum):
    CATALOG = "catalog"
    DISAMBIGUATE = "disambiguate"
    CUSTOM = "custom"


@dataclass(frozen=True)
class AddDecision:
    action: AddAction
    food: Food | None = None
    candidates: tuple[MatchCandidate, ...] = ()


def resolve_add_target(
    typed: str,
    selected_key: str | None,
    foods: Sequence[Food],
    foods_by_id: dict[str, Food],
) -> AddDecision:
    """What 'Add a food' should do with this input, against the FULL catalog
    (including already-stocked foods, so re-adding one tops it up instead of
    silently becoming an orphaned custom item)."""
    food = resolve_food(typed, selected_key, foods, foods_by_id)
    if food is not None:
        return AddDecision(AddAction.CATALOG, food=food)
    level, candidates = match_pantry_input(typed, foods)
    if level == "high" and candidates:
        matched = foods_by_id.get(candidates[0].food_id)
        if matched is not None:
            return AddDecision(AddAction.CATALOG, food=matched)
    if level == "medium" and candidates:
        return AddDecision(AddAction.DISAMBIGUATE, candidates=tuple(candidates))
    return AddDecision(AddAction.CUSTOM)


def apply_catalog_add(pantry: Pantry, food_id: str, grams: float) -> float:
    """Add grams to existing stock (if any) and return the new total."""
    pantry.add(food_id, grams)
    return pantry.items[food_id]


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _fmt_short(value: str) -> str:
    when = _parse_iso(value)
    return f"{when.strftime('%b')} {when.day}" if when else "?"


def _servings_label(servings: float) -> str:
    text = f"{servings:.2f}".rstrip("0").rstrip(".")
    return f"{text} serving left" if servings <= 1.0 + EPSILON else f"{text} servings left"


def _untouched(leftover: PreparedLeftover) -> bool:
    return (
        leftover.status == STATUS_AVAILABLE
        and abs(leftover.servings_remaining - leftover.initial_fraction_remaining) <= EPSILON
    )


def build_pantry_view(page: ft.Page, state: AppState) -> ft.Control:
    foods_by_id = state.foods_by_id
    pantry = state.pantry

    def show_error(message: str) -> None:
        page.show_dialog(ft.SnackBar(ft.Text(message)))

    # -- persistence (every write is one transaction, with memory rollback) --

    def persist_pantry() -> bool:
        snapshot_items = dict(pantry.items)
        snapshot_custom = copy.deepcopy(pantry.custom_items)
        try:
            state.persist(pantry=pantry)
            return True
        except Exception:  # noqa: BLE001 - restore and surface
            pantry.items.clear()
            pantry.items.update(snapshot_items)
            pantry.custom_items[:] = snapshot_custom
            show_error("Couldn't save — nothing was changed.")
            return False

    def commit_leftovers(mutate) -> bool:
        snapshot = copy.deepcopy(state.prepared_leftovers)
        mutate()
        try:
            state.persist(leftovers=state.prepared_leftovers)
            return True
        except Exception:  # noqa: BLE001 - restore and surface
            state.prepared_leftovers[:] = snapshot
            show_error("Couldn't save — nothing was changed.")
            return False

    # -- raw food card grid ---------------------------------------------------

    grid_row = ft.Row(wrap=True, spacing=12, run_spacing=12)
    empty_grid_text = muted_text(
        "Your pantry is empty. Check off purchased groceries from your Plan, "
        "or add foods manually.",
        size=13,
    )

    def pantry_card(food: Food, grams: float) -> ft.Container:
        brand = state.latest_brand_for(food)
        display_unit = preferred_package_unit(
            food, state.saved_plan, state.purchase_log
        )
        amount_text = muted_text(format_grams(food, grams, display_unit), size=12)
        grams_field = ft.TextField(
            value=f"{display_amount(food, grams, display_unit):g}",
            width=124, dense=True, text_size=12.5,
            suffix=ft.Text(
                package_unit_name(display_unit)
                if display_unit is not None else base_unit_name(food),
                size=12,
                color=theme.TEXT_MUTED,
            ),
            keyboard_type=ft.KeyboardType.NUMBER,
            tooltip="Edit this package amount; inventory remains stored in grams",
        )
        style_field(grams_field)
        deleting = {"active": False}

        def on_grams_blur(e) -> None:
            if deleting["active"]:
                return  # the delete button won the race — don't resurrect
            try:
                new_grams = display_amount_to_grams(
                    food, grams_field.value or "", display_unit
                )
            except ValueError:
                grams_field.value = f"{display_amount(food, pantry.items.get(food.id, 0.0), display_unit):g}"
                grams_field.update()
                return
            old_grams = pantry.items.get(food.id, 0.0)
            pantry.set_grams(food.id, max(new_grams, 0.0))  # 0 removes the food
            if not persist_pantry():
                grams_field.value = f"{display_amount(food, old_grams, display_unit):g}"
                grams_field.update()
                return
            if new_grams <= 0:
                rebuild_grid()  # the card disappears
                page.update()
                return
            # Amount-only change: refresh this card, keep scroll/focus intact.
            amount_text.value = format_grams(food, new_grams, display_unit)
            card_container.update()

        grams_field.on_blur = on_grams_blur

        def on_delete(e) -> None:
            deleting["active"] = True
            grams_field.disabled = True  # a pending blur can't re-add the food
            old_grams = pantry.items.get(food.id, 0.0)
            pantry.set_grams(food.id, 0.0)
            if not persist_pantry():
                pantry.set_grams(food.id, old_grams)
                deleting["active"] = False
                grams_field.disabled = False
                page.update()
                return
            rebuild_grid()
            page.update()

        card_container = ft.Container(
            width=theme.CARD_GRID_ITEM_WIDTH,
            bgcolor=theme.SURFACE,
            border=ft.Border.all(1, theme.BORDER),
            border_radius=theme.RADIUS_SM,
            padding=12,
            content=ft.Column(
                [
                    food_photo(
                        food, theme.CARD_GRID_ITEM_WIDTH - 24, 110,
                        image_src=state.user_image_src_for(food),
                    ),
                    ft.Text(
                        food.name, size=13.5, weight=ft.FontWeight.W_600, color=theme.TEXT,
                        max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    *(
                        [muted_text(brand, size=11.5)]
                        if brand
                        else []
                    ),
                    amount_text,
                    ft.Row(
                        [
                            grams_field,
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_size=18,
                                icon_color=theme.TEXT_MUTED,
                                tooltip="Remove from pantry",
                                on_click=on_delete,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=6,
            ),
        )
        return card_container

    def rebuild_grid() -> None:
        cards = [
            pantry_card(foods_by_id[food_id], grams)
            for food_id, grams in sorted(pantry.items.items())
            if food_id in foods_by_id
        ]
        grid_row.controls = cards
        empty_grid_text.visible = not cards
        refresh_add_options()

    # -- add a food (type-to-filter) ------------------------------------------

    # All four controls in the add row share CONTROL_HEIGHT so the row reads as
    # one unit (the dropdown, amount field, and both buttons line up).
    CONTROL_HEIGHT = 46
    add_dropdown = ft.Dropdown(
        label="Add a food", width=260, text_size=13, height=CONTROL_HEIGHT,
        editable=True, enable_filter=True, enable_search=True, menu_height=320,
    )
    add_grams_field = ft.TextField(
        label="Amount (g)", width=130, text_size=13, height=CONTROL_HEIGHT,
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    for field in (add_dropdown, add_grams_field):
        style_field(field)

    def refresh_add_options() -> None:
        add_dropdown.options = [
            ft.DropdownOption(
                key=f.id,
                text=(
                    f"{f.name} — {carryover_amount_label(f, pantry.items[f.id])} in pantry"
                    if f.id in pantry.items
                    else f.name
                ),
            )
            for f in sorted_catalog_foods(state.foods, pantry.items)
        ]

    def parse_add_grams() -> float | None:
        """The typed amount in grams, or None (with an inline error) if invalid."""
        try:
            grams = normalize_grams(add_grams_field.value or "", positive=True)
            add_grams_field.error_text = None
            return grams
        except ValueError:
            add_grams_field.error_text = "Enter a positive amount"
            page.update()
            return None

    def clear_add_row() -> None:
        add_dropdown.value = None
        add_dropdown.text = ""
        add_dropdown.error_text = None
        add_grams_field.value = ""
        add_grams_field.error_text = None

    def add_catalog_food(food: Food, grams: float) -> None:
        new_total = apply_catalog_add(pantry, food.id, grams)
        if not persist_pantry():
            return
        clear_add_row()
        rebuild_grid()
        page.show_dialog(ft.SnackBar(ft.Text(
            f"Added {carryover_amount_label(food, grams)}. "
            f"{food.name} now has {carryover_amount_label(food, new_total)}."
        )))
        page.update()

    def on_add(e) -> None:
        typed = (add_dropdown.text or "").strip()
        grams = parse_add_grams()
        if grams is None:
            return
        decision = resolve_add_target(
            typed, add_dropdown.value, sorted_catalog_foods(state.foods, pantry.items),
            foods_by_id,
        )
        add_dropdown.error_text = None
        if decision.action is AddAction.CATALOG:
            add_catalog_food(decision.food, grams)
        elif decision.action is AddAction.DISAMBIGUATE:
            open_disambiguation_dialog(typed, grams, list(decision.candidates))
        else:
            # No reliable match: save it as a custom item (never silently linked).
            open_custom_create_dialog(typed, grams)

    def on_dropdown_select(e) -> None:
        add_dropdown.error_text = None

    add_dropdown.on_change = on_dropdown_select
    add_button = primary_button("Add", icon=ft.Icons.ADD)
    add_button.height = CONTROL_HEIGHT
    add_button.on_click = on_add

    # -- add by photo (OpenAI vision) ------------------------------------------

    file_picker = ensure_file_picker(page)

    def on_photo_committed() -> None:
        rebuild_all()
        page.update()

    async def on_add_photo(e) -> None:
        await run_product_photo_flow(page, state, file_picker, on_photo_committed)

    async def on_scan_receipt(e) -> None:
        await run_receipt_flow(page, state, file_picker, on_photo_committed)

    photo_button = ft.FilledTonalButton(
        content="Add by photo",
        icon=ft.Icons.PHOTO_CAMERA_OUTLINED,
        height=CONTROL_HEIGHT,
        on_click=on_add_photo,
        tooltip="Photograph a purchased food — AI fills in the details for review",
    )

    receipt_button = ft.FilledTonalButton(
        content="Scan receipt",
        icon=ft.Icons.RECEIPT_LONG_OUTLINED,
        height=CONTROL_HEIGHT,
        on_click=on_scan_receipt,
        tooltip="Scan receipt foods; only uncertain items need review",
    )

    # -- custom items (not linked to the catalog) ------------------------------

    custom_row = ft.Column(spacing=10)
    empty_custom_text = muted_text(
        "Foods that don't match the catalog are saved here. They're kept as a "
        "reminder but aren't used in meal planning until you link them.",
        size=13,
    )

    def refresh_custom_items() -> None:
        items = sorted(pantry.pending_custom_items(), key=lambda c: c.display_name.lower())
        custom_row.controls = [custom_item_card(item) for item in items]
        empty_custom_text.visible = not items

    def rebuild_all() -> None:
        """Rebuild every Pantry surface affected by one photo transaction."""

        rebuild_grid()  # catalog cards, empty state, and add-option annotations
        refresh_custom_items()  # Custom cards and Custom empty state

    def save_custom_item(item: CustomPantryItem) -> bool:
        pantry.add_custom_item(item)
        if not persist_pantry():
            pantry.remove_custom_item(item.id)
            return False
        clear_add_row()
        refresh_custom_items()
        rebuild_grid()
        page.update()
        return True

    def open_disambiguation_dialog(typed: str, grams: float, candidates) -> None:
        """Ambiguous input: let the user pick a catalog food, or keep it custom.
        Nothing is added until the user confirms one of the choices."""
        def pick(food_id: str):
            def handler(ev) -> None:
                page.pop_dialog()
                food = foods_by_id.get(food_id)
                if food is not None:
                    add_catalog_food(food, grams)
            return handler

        choice_buttons = [
            ft.TextButton(
                content=ft.Text(c.display, color=theme.PRIMARY_DARK),
                icon=ft.Icons.CHECK,
                on_click=pick(c.food_id),
            )
            for c in candidates[:5]
        ]

        def keep_custom(ev) -> None:
            page.pop_dialog()
            open_custom_create_dialog(typed, grams)

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("Did you mean one of these?", size=15,
                          weight=ft.FontWeight.W_600, color=theme.TEXT),
            content=ft.Container(
                width=340,
                content=ft.Column(
                    [
                        muted_text(
                            f"“{typed}” matches a few catalog foods. Pick the right "
                            "one, or keep it as a custom item.",
                            size=12.5,
                        ),
                        *choice_buttons,
                    ],
                    spacing=8,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton(content="None of these — keep as custom", on_click=keep_custom),
                ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
            ],
        ))

    def open_custom_create_dialog(prefill_name: str, prefill_grams: float) -> None:
        name_field = ft.TextField(label="Name", value=prefill_name, width=300, dense=True,
                                  text_size=12.5)
        amount_field = ft.TextField(label="Amount", value=f"{prefill_grams:g}", width=120,
                                    dense=True, text_size=12.5,
                                    keyboard_type=ft.KeyboardType.NUMBER)
        unit_field = ft.TextField(label="Unit", value="g", width=100, dense=True, text_size=12.5)
        grams_field = ft.TextField(label="Estimated grams", value=f"{prefill_grams:g}", width=150,
                                   dense=True, text_size=12.5,
                                   keyboard_type=ft.KeyboardType.NUMBER)
        brand_field = ft.TextField(label="Brand (optional)", width=220, dense=True, text_size=12.5)
        price_field = ft.TextField(label="Price (optional)", width=140, dense=True, text_size=12.5,
                                   keyboard_type=ft.KeyboardType.NUMBER)
        expiry_field = ft.TextField(label="Expiration (YYYY-MM-DD, optional)", width=260,
                                    dense=True, text_size=12.5)
        for f in (name_field, amount_field, unit_field, grams_field, brand_field,
                  price_field, expiry_field):
            style_field(f)

        def on_save(ev) -> None:
            name = (name_field.value or "").strip()
            if not name:
                name_field.error_text = "Enter a name"
                page.update()
                return
            try:
                amount = float(amount_field.value or "0")
            except ValueError:
                amount = 0.0
            try:
                grams_est = max(0.0, float(grams_field.value or "0"))
            except ValueError:
                grams_est = 0.0
            price = None
            if (price_field.value or "").strip():
                try:
                    price = float(price_field.value)
                except ValueError:
                    price = None
            expiration = (expiry_field.value or "").strip()
            if expiration and _parse_iso(expiration) is None:
                expiry_field.error_text = "Use YYYY-MM-DD"
                page.update()
                return
            item = CustomPantryItem(
                id=f"{CUSTOM_ID_PREFIX}{uuid.uuid4().hex}",
                original_name=prefill_name,
                display_name=name,
                amount=amount,
                unit=(unit_field.value or "").strip() or "g",
                grams_estimate=grams_est,
                brand=(brand_field.value or "").strip(),
                price=price,
                expiration=expiration,
                mapping_status=MAPPING_PENDING,
                canonical_food_id=None,
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
            page.pop_dialog()
            save_custom_item(item)

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("Add a custom item", size=15, weight=ft.FontWeight.W_600,
                          color=theme.TEXT),
            content=ft.Container(
                width=360,
                content=ft.Column(
                    [
                        muted_text(
                            "We couldn't match this to a catalog food. It'll be saved "
                            "as a reminder — not used in meal planning until you link it.",
                            size=12.5,
                        ),
                        name_field,
                        ft.Row([amount_field, unit_field, grams_field], spacing=8, wrap=True),
                        ft.Row([brand_field, price_field], spacing=8, wrap=True),
                        expiry_field,
                    ],
                    spacing=10,
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
                ft.TextButton(content="Save custom item", on_click=on_save),
            ],
        ))

    def open_link_dialog(item: CustomPantryItem) -> None:
        """Explicitly link a custom item to a catalog food. Suggested matches are
        offered for review; linking only happens on an explicit Confirm — a
        custom item is never linked silently."""
        query = item.original_name or item.display_name
        level, candidates = match_pantry_input(query, list(state.foods))
        selected = {"food_id": candidates[0].food_id if candidates else None}

        catalog_dropdown = ft.Dropdown(
            label="Or pick any catalog food", width=320, text_size=12.5,
            editable=True, enable_filter=True, enable_search=True, menu_height=280,
            options=[ft.DropdownOption(key=f.id, text=f.name)
                     for f in sorted(state.foods, key=lambda f: f.name)],
        )
        style_field(catalog_dropdown)

        suggestion_rows: list[ft.Control] = []
        radios: list[ft.Radio] = []
        for c in candidates[:5]:
            radios.append(ft.Radio(value=c.food_id, label=c.display))
        radio_group = ft.RadioGroup(
            value=selected["food_id"],
            content=ft.Column(radios, spacing=2, tight=True),
        )

        def on_radio_change(ev) -> None:
            selected["food_id"] = radio_group.value
            catalog_dropdown.value = None

        radio_group.on_change = on_radio_change

        def on_dropdown_change(ev) -> None:
            if catalog_dropdown.value:
                selected["food_id"] = catalog_dropdown.value
                radio_group.value = None
                page.update()

        catalog_dropdown.on_change = on_dropdown_change

        if candidates:
            suggestion_rows.append(muted_text("Suggested matches — review before confirming:",
                                              size=12.5))
            suggestion_rows.append(radio_group)
        else:
            suggestion_rows.append(muted_text(
                "No close catalog match. Pick a food to link, or keep it custom.", size=12.5))

        def on_confirm(ev) -> None:
            food_id = catalog_dropdown.value or selected["food_id"]
            if not food_id or food_id not in foods_by_id:
                page.show_dialog(ft.SnackBar(ft.Text("Pick a food to link to first.")))
                return
            page.pop_dialog()
            if pantry.link_custom_item(item.id, food_id):
                if persist_pantry():
                    refresh_custom_items()
                    rebuild_grid()  # the linked grams now show as a catalog food
                    page.update()

        page.show_dialog(ft.AlertDialog(
            title=ft.Text(f"Link “{item.display_name}” to the catalog", size=15,
                          weight=ft.FontWeight.W_600, color=theme.TEXT),
            content=ft.Container(
                width=360,
                content=ft.Column(
                    [
                        muted_text(
                            f"Linking adds {item.grams_estimate:g} g to the catalog food "
                            "and lets meal planning use it. This can't be undone silently — "
                            "confirm only if it's really the same food.",
                            size=12.5,
                        ),
                        *suggestion_rows,
                        catalog_dropdown,
                    ],
                    spacing=10,
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.TextButton(content="Not this item — keep custom",
                              on_click=lambda ev: page.pop_dialog()),
                ft.TextButton(content="Confirm link", on_click=on_confirm),
            ],
        ))

    def custom_item_card(item: CustomPantryItem) -> ft.Container:
        def on_remove(ev) -> None:
            snapshot = copy.deepcopy(pantry.custom_items)
            pantry.remove_custom_item(item.id)
            if not persist_pantry():
                pantry.custom_items[:] = snapshot
                return
            refresh_custom_items()
            page.update()

        async def on_replace_image(ev) -> None:
            files = await file_picker.pick_files(
                dialog_title="Choose a Custom Pantry image",
                allowed_extensions=["jpg", "jpeg", "png"],
                allow_multiple=False,
            )
            if not files or not files[0].path:
                return
            processing = ImageProcessingView(
                page, title="Processing Custom Pantry image"
            )
            processing.show(
                "Preparing the uploaded image",
                "The image is being decoded, orientation-corrected, and sanitized.",
            )
            try:
                normalized = normalize_image(Path(files[0].path).read_bytes())
            except OSError:
                processing.show_failure(ImageFailureDetails(
                    summary="The selected image file could not be opened.",
                    stage="File access",
                    reason="The file may have moved, be locked, or be unreadable.",
                    suggestions=("Choose the image again from a local folder.",),
                ))
                return
            except ValueError as exc:
                processing.show_failure(ImageFailureDetails(
                    summary="The selected file is not a supported image.",
                    stage="Image normalization",
                    reason=str(exc),
                    suggestions=(
                        "Use a valid JPG or PNG image.",
                        "Choose a smaller image if the file is unusually large.",
                    ),
                ))
                return
            relative = (
                f"{IMPORTED_IMAGES_DIRNAME}/custom-{uuid.uuid4()}"
                f"{normalized.extension}"
            )
            final_path = state.store.base_dir / relative
            previous = (
                item.image_path,
                item.image_source,
                item.image_source_page,
                item.image_author,
                item.image_license,
                item.image_license_url,
            )
            processing.show(
                "Saving the Custom Pantry image",
                "The sanitized image is being saved to local application storage.",
            )
            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = final_path.parent / f".tmp-{final_path.name}"
                temporary.write_bytes(normalized.content)
                os.replace(temporary, final_path)
                item.image_path = relative
                item.image_source = "user_upload"
                item.image_source_page = None
                item.image_author = None
                item.image_license = None
                item.image_license_url = None
                state.persist(pantry=pantry)
            except Exception:
                (
                    item.image_path,
                    item.image_source,
                    item.image_source_page,
                    item.image_author,
                    item.image_license,
                    item.image_license_url,
                ) = previous
                try:
                    final_path.unlink()
                except OSError:
                    pass
                processing.show_failure(ImageFailureDetails(
                    summary="The image was processed but could not be saved.",
                    stage="Local image storage",
                    reason="The application could not complete the local file transaction.",
                    suggestions=(
                        "Check available disk space and folder permissions.",
                        "Retry the upload.",
                    ),
                ))
                return
            processing.close()
            refresh_custom_items()
            page.update()

        async def on_find_licensed_image(ev) -> None:
            try:
                results = await WikimediaImageSearch(state.http_client).search(
                    item.display_name, limit=8
                )
            except Exception:
                results = ()
            if not results:
                page.show_dialog(ft.SnackBar(ft.Text(
                    "No licensed Wikimedia images are available right now; the "
                    "food-group placeholder will remain."
                )))
                return

            result_rows: list[ft.Control] = []
            for result in results:
                async def select_result(event, chosen=result) -> None:
                    try:
                        normalized = await WikimediaImageSearch(
                            state.http_client
                        ).download(chosen)
                        relative = (
                            f"{IMPORTED_IMAGES_DIRNAME}/custom-{uuid.uuid4()}"
                            f"{normalized.extension}"
                        )
                        final_path = state.store.base_dir / relative
                        final_path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = final_path.parent / f".tmp-{final_path.name}"
                        temporary.write_bytes(normalized.content)
                        os.replace(temporary, final_path)
                        previous = copy.deepcopy(item)
                        item.image_path = relative
                        item.image_source = "wikimedia"
                        item.image_source_page = chosen.source_page
                        item.image_author = chosen.author
                        item.image_license = chosen.license_name
                        item.image_license_url = chosen.license_url
                        try:
                            state.persist(pantry=pantry)
                        except Exception:
                            item.__dict__.update(previous.__dict__)
                            try:
                                final_path.unlink()
                            except OSError:
                                pass
                            raise
                    except Exception:
                        page.show_dialog(ft.SnackBar(ft.Text(
                            "That Wikimedia image could not be validated or saved."
                        )))
                        return
                    page.pop_dialog()
                    refresh_custom_items()
                    page.update()

                result_rows.append(ft.Row([
                    ft.Image(
                        src=result.thumbnail_url,
                        width=84,
                        height=64,
                        fit=ft.BoxFit.CONTAIN,
                    ),
                    ft.Column([
                        ft.Text(result.title, size=11.5, max_lines=2),
                        muted_text(
                            f"{result.author} · {result.license_name}", size=10.5
                        ),
                    ], spacing=2, expand=True),
                    ft.TextButton(content="Use this image", on_click=select_result),
                ], spacing=8))
            page.show_dialog(ft.AlertDialog(
                title=ft.Text("Choose a licensed Wikimedia image"),
                content=ft.Container(
                    width=560,
                    height=420,
                    content=ft.Column(
                        result_rows, spacing=8, scroll=ft.ScrollMode.AUTO
                    ),
                ),
                actions=[
                    ft.TextButton(content="Keep placeholder", on_click=lambda event: page.pop_dialog())
                ],
            ))

        meta_bits = []
        if item.amount:
            meta_bits.append(f"{item.amount:g} {item.unit}".strip())
        if item.brand:
            meta_bits.append(item.brand)
        if item.expiration:
            meta_bits.append(f"use by {_fmt_short(item.expiration)}")
        meta_line = " · ".join(meta_bits)

        level, candidates = match_pantry_input(
            item.original_name or item.display_name, list(state.foods)
        )
        if level in ("high", "medium") and candidates:
            status_pill = pill(
                f"Looks like {candidates[0].display} — tap Link to add it",
                theme.PRIMARY_TINT, theme.PRIMARY_DARK,
            )
        else:
            status_pill = pill(
                "Not linked to catalog — not used in meal planning",
                theme.WARN_BG, theme.WARN_INK,
            )

        image_path = state.store.base_dir / item.image_path if item.image_path else None
        if image_path is not None and image_path.is_file():
            image_control: ft.Control = ft.Container(
                content=ft.Image(src=str(image_path), fit=ft.BoxFit.COVER),
                width=64,
                height=64,
                border_radius=theme.RADIUS_SM,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            )
        else:
            image_control = ft.Container(
                content=ft.Icon(ft.Icons.HELP_OUTLINE, size=22, color=theme.TEXT_MUTED),
                width=64, height=64, bgcolor=theme.SURFACE_TINT,
                border_radius=theme.RADIUS_SM, alignment=ft.Alignment.CENTER,
            )
        image_button = ft.TextButton(
            content="Replace image" if item.image_path else "Add image",
            icon=ft.Icons.IMAGE_OUTLINED,
            on_click=on_replace_image,
        )
        licensed_image_button = ft.TextButton(
            content="Find licensed image",
            icon=ft.Icons.PUBLIC,
            on_click=on_find_licensed_image,
        )
        attribution = (
            muted_text(
                f"Image: {item.image_author or 'Wikimedia contributor'} · "
                f"{item.image_license or 'license details saved'}",
                size=10.5,
            )
            if item.image_source == "wikimedia" else None
        )

        return ft.Container(
            bgcolor=theme.SURFACE,
            border=ft.Border.all(1, theme.BORDER),
            border_radius=theme.RADIUS_SM,
            padding=12,
            content=ft.Row(
                [
                    image_control,
                    ft.Column(
                        [
                            ft.Text(item.display_name, size=13.5, weight=ft.FontWeight.W_600,
                                    color=theme.TEXT),
                            *( [muted_text(meta_line, size=12)] if meta_line else [] ),
                            status_pill,
                            *([attribution] if attribution is not None else []),
                        ],
                        spacing=4,
                        expand=True,
                    ),
                    ft.TextButton(
                        content="Link to catalog ingredient",
                        icon=ft.Icons.LINK,
                        on_click=lambda ev, it=item: open_link_dialog(it),
                    ),
                    image_button,
                    licensed_image_button,
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE, icon_size=18, icon_color=theme.TEXT_MUTED,
                        tooltip="Remove this custom item", on_click=on_remove,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    # -- prepared meals ---------------------------------------------------------

    leftovers_row = ft.Row(wrap=True, spacing=12, run_spacing=12)
    empty_leftovers_text = muted_text(
        "No prepared meals yet. Report leftovers on a meal card (Plan tab) and "
        "they show up here, ready to eat or to be scheduled into your next plan.",
        size=13,
    )

    def visible_leftovers() -> list[PreparedLeftover]:
        items = [lo for lo in state.prepared_leftovers if lo.status == STATUS_AVAILABLE]
        return sorted(items, key=lambda lo: (lo.use_by_date, lo.prepared_at, lo.id))

    def refresh_leftovers() -> None:
        cards = [leftover_card(lo) for lo in visible_leftovers()]
        leftovers_row.controls = cards
        empty_leftovers_text.visible = not cards

    def leftover_card(leftover: PreparedLeftover) -> ft.Container:
        reserved = reserved_slot_for(state.saved_plan, leftover.id)
        past_use_by = (_parse_iso(leftover.use_by_date) or date.max) < date.today()

        def eat_servings(amount: float) -> None:
            def mutate() -> None:
                factor = max(
                    (leftover.servings_remaining - amount) / leftover.servings_remaining, 0.0
                )
                for portion in leftover.portions:
                    portion.remaining_grams *= factor
                refresh_derived_fields(leftover, foods_by_id)  # flips to consumed at ~0

            if commit_leftovers(mutate):
                refresh_leftovers()
                page.update()

        def open_eat_dialog(e) -> None:
            amount_field = ft.TextField(
                label="Servings eaten",
                value=f"{leftover.servings_remaining:.2f}".rstrip("0").rstrip("."),
                width=160, dense=True, text_size=12.5,
                keyboard_type=ft.KeyboardType.NUMBER,
            )
            style_field(amount_field)
            notes: list[ft.Control] = [
                muted_text("Eating a prepared meal never touches raw pantry stock.", size=12.5)
            ]
            if past_use_by:
                notes.append(muted_text("This is past the suggested use-by date.", size=12.5))

            def on_confirm(ev) -> None:
                try:
                    amount = float(amount_field.value or "")
                    if not 0 < amount <= leftover.servings_remaining + EPSILON:
                        raise ValueError
                except ValueError:
                    amount_field.error_text = "Up to what's left"
                    page.update()
                    return
                page.pop_dialog()
                eat_servings(min(amount, leftover.servings_remaining))

            page.show_dialog(ft.AlertDialog(
                title=ft.Text(f"Eat {leftover.meal_name}?", size=15,
                              weight=ft.FontWeight.W_600, color=theme.TEXT),
                content=ft.Container(
                    width=320,
                    content=ft.Column([*notes, amount_field], spacing=10, tight=True),
                ),
                actions=[
                    ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
                    ft.TextButton(content="Eat", on_click=on_confirm),
                ],
            ))

        def on_discard(e) -> None:
            def mutate() -> None:
                leftover.status = STATUS_DISCARDED

            if commit_leftovers(mutate):
                refresh_leftovers()
                page.update()

        def open_adjust_dialog(e) -> None:
            current = leftover.servings_remaining
            amount_field = ft.TextField(
                label=f"Servings left (0–{current:.2f})",
                value=f"{current:.2f}".rstrip("0").rstrip("."),
                width=180, dense=True, text_size=12.5,
                keyboard_type=ft.KeyboardType.NUMBER,
            )
            style_field(amount_field)

            def on_confirm(ev) -> None:
                try:
                    new_servings = float(amount_field.value or "")
                    if not 0 <= new_servings <= current + EPSILON:
                        raise ValueError
                except ValueError:
                    amount_field.error_text = "Only down, never up"
                    page.update()
                    return
                page.pop_dialog()
                eat_servings(current - min(new_servings, current))

            page.show_dialog(ft.AlertDialog(
                title=ft.Text("Adjust remaining amount", size=15,
                              weight=ft.FontWeight.W_600, color=theme.TEXT),
                content=ft.Container(
                    width=320,
                    content=ft.Column(
                        [
                            muted_text(
                                "You can only reduce what's left — cooked food "
                                "can't reappear.",
                                size=12.5,
                            ),
                            amount_field,
                        ],
                        spacing=10,
                        tight=True,
                    ),
                ),
                actions=[
                    ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
                    ft.TextButton(content="Save", on_click=on_confirm),
                ],
            ))

        def open_estimate_dialog(e) -> None:
            percent_field = ft.TextField(
                label="Percent of a serving left (0–100)",
                value=f"{leftover.initial_fraction_remaining * 100:.0f}",
                width=220, dense=True, text_size=12.5,
                keyboard_type=ft.KeyboardType.NUMBER,
            )
            style_field(percent_field)

            def on_confirm(ev) -> None:
                try:
                    pct = float(percent_field.value or "")
                    if not 0 <= pct <= 100:
                        raise ValueError
                except ValueError:
                    percent_field.error_text = "Enter 0–100"
                    page.update()
                    return
                fraction = pct / 100.0

                def mutate() -> None:
                    for portion in leftover.portions:
                        portion.remaining_grams = portion.original_grams * fraction
                    leftover.initial_fraction_remaining = fraction
                    refresh_derived_fields(leftover, foods_by_id)

                page.pop_dialog()
                if commit_leftovers(mutate):
                    refresh_leftovers()
                    page.update()

            page.show_dialog(ft.AlertDialog(
                title=ft.Text("Correct the original estimate", size=15,
                              weight=ft.FontWeight.W_600, color=theme.TEXT),
                content=ft.Container(
                    width=320,
                    content=ft.Column(
                        [
                            muted_text(
                                "Rewrites how much was left in the first place. "
                                "Only possible while nothing has been eaten from "
                                "this record.",
                                size=12.5,
                            ),
                            percent_field,
                        ],
                        spacing=10,
                        tight=True,
                    ),
                ),
                actions=[
                    ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
                    ft.TextButton(content="Save", on_click=on_confirm),
                ],
            ))

        def from_dialog(action):
            """Close the detail dialog before running an action (which may open
            its own dialog — the two must never stack)."""
            def handler(ev) -> None:
                page.pop_dialog()
                action(ev)

            return handler

        def open_detail_dialog(e) -> None:
            rows: list[ft.Control] = []
            header_pills: list[ft.Control] = []
            if leftover.origin_kind == ORIGIN_BATCH:
                header_pills.append(pill("batch", theme.SURFACE_TINT, theme.TEXT_MUTED,
                                         tooltip="The second serving of a batch-cooked dinner"))
            if past_use_by:
                header_pills.append(pill("past suggested use-by", theme.WARN_BG, theme.WARN_INK,
                                         tooltip="A freshness suggestion, not a safety rating"))
            if header_pills:
                rows.append(ft.Row(header_pills, spacing=6, wrap=True))
            rows.append(muted_text(_servings_label(leftover.servings_remaining), size=12.5))
            component_label = component_summary_label(leftover)
            if component_label:
                rows.append(muted_text(f"Contains: {component_label.lower()}", size=12.5))
            rows.append(muted_text(
                f"Made {_fmt_short(leftover.prepared_at)} · suggested use by "
                f"{_fmt_short(leftover.use_by_date)}",
                size=12.5,
            ))
            if leftover.note:
                rows.append(ft.Text(f"“{leftover.note}”", size=12.5, color=theme.TEXT_MUTED))

            if reserved is not None:
                when, slot = reserved
                rows.append(muted_text(
                    f"Reserved for {when.strftime('%A')} {slot.value} in the current "
                    "plan — regenerate the plan or eat that meal first.",
                    size=12.5,
                ))
            else:
                actions: list[ft.Control] = [
                    ft.TextButton(content="Eat now", icon=ft.Icons.RESTAURANT,
                                  on_click=from_dialog(open_eat_dialog)),
                    ft.Container(expand=True),
                    ft.IconButton(
                        icon=ft.Icons.TUNE, icon_size=16, icon_color=theme.TEXT_MUTED,
                        tooltip="Adjust remaining amount (only down)",
                        on_click=from_dialog(open_adjust_dialog),
                    ),
                ]
                if _untouched(leftover):
                    actions.append(ft.IconButton(
                        icon=ft.Icons.EDIT_OUTLINED, icon_size=16, icon_color=theme.TEXT_MUTED,
                        tooltip="Correct the original estimate",
                        on_click=from_dialog(open_estimate_dialog),
                    ))
                actions.append(ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE, icon_size=16, icon_color=theme.TEXT_MUTED,
                    tooltip="Discard these leftovers",
                    on_click=from_dialog(on_discard),
                ))
                rows.append(ft.Row(actions, spacing=0,
                                   vertical_alignment=ft.CrossAxisAlignment.CENTER))

            page.show_dialog(ft.AlertDialog(
                title=ft.Text(leftover.meal_name, size=15,
                              weight=ft.FontWeight.W_600, color=theme.TEXT),
                content=ft.Container(
                    width=320,
                    content=ft.Column(rows, spacing=10, tight=True),
                ),
                actions=[ft.TextButton(content="Close",
                                       on_click=lambda ev: page.pop_dialog())],
            ))

        # Card face: photo of the largest remaining portion, name, what's left,
        # and the dates. Everything else lives in the tap-to-open detail dialog.
        photo_width = theme.CARD_GRID_ITEM_WIDTH - 24
        candidates = [p for p in leftover.portions if p.food_id in foods_by_id]
        largest = max(candidates, key=lambda p: p.remaining_grams, default=None)
        if largest is not None:
            food = foods_by_id[largest.food_id]
            photo: ft.Control = food_photo(
                food, photo_width, 110, image_src=state.image_src_for(food)
            )
        else:
            photo = ft.Container(
                content=ft.Icon(ft.Icons.RESTAURANT, size=44, color=theme.TEXT_MUTED),
                width=photo_width,
                height=110,
                border_radius=theme.RADIUS_SM,
                bgcolor=theme.SURFACE_TINT,
                alignment=ft.Alignment.CENTER,
            )

        return ft.Container(
            width=theme.CARD_GRID_ITEM_WIDTH,
            bgcolor=theme.SURFACE,
            border=ft.Border.all(1, theme.BORDER),
            border_radius=theme.RADIUS_SM,
            padding=12,
            ink=True,
            on_click=open_detail_dialog,
            tooltip="View details",
            content=ft.Column(
                [
                    photo,
                    ft.Text(
                        leftover.meal_name, size=13.5, weight=ft.FontWeight.W_600,
                        color=theme.TEXT, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    muted_text(_servings_label(leftover.servings_remaining), size=12),
                    muted_text(
                        f"Made {_fmt_short(leftover.prepared_at)} · use by "
                        f"{_fmt_short(leftover.use_by_date)}",
                        size=12,
                    ),
                ],
                spacing=6,
            ),
        )

    rebuild_all()
    refresh_leftovers()

    pantry_card_section = section_card(
        "My Pantry",
        empty_grid_text,
        grid_row,
        ft.Divider(height=1, color=theme.BORDER),
        ft.Row(
            [add_dropdown, add_grams_field, add_button, photo_button, receipt_button],
            wrap=True,
            spacing=10,
            run_spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        subtitle=(
            "Raw ingredients you already have at home. Your next plan uses "
            "these foods first — they cost nothing."
        ),
    )
    custom_section = section_card(
        "Custom items",
        empty_custom_text,
        custom_row,
        subtitle=(
            "Foods that didn't match the catalog. They're saved as reminders and "
            "stay out of meal planning until you link them to a catalog ingredient."
        ),
    )
    prepared_section = section_card(
        "Prepared meals",
        empty_leftovers_text,
        leftovers_row,
        subtitle=(
            "Cooked leftovers, in servings. Your next plan schedules them into "
            "meal slots automatically — they never turn back into raw ingredients."
        ),
    )
    # STRETCH here is the horizontal (cross) axis, which is bounded — both
    # section cards always span the full tab width regardless of content.
    return ft.Column(
        [pantry_card_section, custom_section, prepared_section],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
