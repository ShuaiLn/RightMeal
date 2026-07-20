"""Plan page: renders the saved plan — basket, daily meals, and nutrition."""

from __future__ import annotations

from datetime import date
from typing import Callable, Sequence

import flet as ft

import theme
from models import (
    PRICE_SOURCE_LABELS,
    Explanation,
    Food,
    FoodGroup,
    Nutrients,
    PlanKind,
    PriceSource,
    SavedBasketItem,
    SavedPlan,
)
from models.purchase_log import (
    ORIGIN_DIRECT_BUTTON,
    PurchaseInput,
    PurchaseRecord,
    new_purchase_event_id,
)
from planner import pantry_used_totals, prepared_meal_totals
from services.pantry_flow import (
    can_void_group,
    purchased_value,
    record_purchase_events,
    void_purchase_group,
)
from services.package_units import format_grams, package_unit, preferred_package_unit
from services.source_allocation import (
    GRAM_EPSILON,
    FoodAllocation,
    allocate_sources,
    covered_nutrients,
    dynamic_open_cost,
    is_historical,
)
from ui.components import (
    GapRow,
    budget_bar,
    card,
    collapsible_section,
    food_avatar,
    muted_text,
    nutrition_card,
    pill,
    primary_button,
    source_mix_caption,
    warnings_strip,
)
from ui.meals_section import (
    carryover_amount_label,
    daily_meals_section,
    partial_plan_warning_banner,
    stale_plan_warning_banner,
)
from ui.photo_purchase import ensure_file_picker, run_receipt_flow
from ui.state import AppState


ESTIMATED_CAP_DISCLAIMER = (
    "Prices may mix stores. The estimated basket budget cap excludes tax, "
    "delivery fees, minimum-order requirements, membership conditions, and "
    "price changes."
)

SINGLE_PACKAGE_CARD_HEIGHT = 124
PACKAGE_ROW_HEIGHT = 50
EXTRA_PACKAGE_GROUP_HEIGHT = PACKAGE_ROW_HEIGHT + 8


def basket_source_mix(items: tuple[SavedBasketItem, ...]) -> dict[PriceSource, int]:
    """Count every saved offer row, including multiple offers for one food."""

    result: dict[PriceSource, int] = {}
    for item in items:
        try:
            source = PriceSource(item.source)
        except ValueError:
            # A corrupt source is rejected by the store.  This guard only keeps
            # legacy in-memory plans renderable while still disclosing an
            # estimate rather than silently assigning retailer provenance.
            source = PriceSource.SEED_ESTIMATE
        result[source] = result.get(source, 0) + 1
    return result


def plan_purchase_groups(
    plan: SavedPlan,
    purchase_log: list[PurchaseRecord],
) -> tuple[tuple[str, tuple[PurchaseRecord, ...]], ...]:
    """Every non-void purchase event group applied to ``plan``.

    A purchase group is an immutable user action (a direct package click or a
    receipt import).  Basket-row completion is deliberately not inferred from
    food-level purchased grams.
    """

    relevant_groups = {
        record.group_id or record.event_id
        for record in purchase_log
        if record.plan_id == plan.plan_id and record.voided_at is None
    }
    grouped: dict[str, list[PurchaseRecord]] = {}
    for record in purchase_log:
        group_id = record.group_id or record.event_id
        if record.voided_at is not None or group_id not in relevant_groups:
            continue
        # A receipt may contain both plan-applied and pantry-only lines.  Undo
        # operates on the whole group, so the history must disclose every line
        # that the button will affect.
        grouped.setdefault(group_id, []).append(record)
    return tuple((group_id, tuple(records)) for group_id, records in grouped.items())


def saved_item_quantity_label(food: Food, item: SavedBasketItem) -> str:
    """Render historical weight from the saved snapshot, never live catalog data."""

    count_prefix = "" if item.count == 1 else f"{item.count} × "
    label = f"{count_prefix}{item.package_label}"
    if item.package_grams <= 0:
        return label
    per_package = format_grams(food, item.package_grams)
    if per_package.casefold() not in item.package_label.casefold():
        label += f" · {per_package} each"
    return label


def compact_package_label(label: str) -> str:
    """Short card label while the details dialog retains the full snapshot."""

    normalized = label.strip()
    parenthetical = normalized.rfind(" (")
    if parenthetical > 0 and normalized.endswith(")"):
        return normalized[:parenthetical]
    return normalized


def package_quantity_label(package_label: str, count: int) -> str:
    prefix = "" if count == 1 else f"{count} × "
    return f"{prefix}{compact_package_label(package_label)}"


def pantry_quantity_suffix(
    food: Food,
    from_pantry: float,
    package_label: str,
) -> str | None:
    """Active-plan pantry coverage in the package unit bound to this food."""

    if from_pantry <= GRAM_EPSILON:
        return None
    try:
        unit = package_unit(food, package_label)
    except ValueError:
        unit = None
    amount = (
        format_grams(food, from_pantry, unit)
        if unit is not None
        else carryover_amount_label(food, from_pantry)
    )
    return f"+ {amount} in my pantry"


def package_line_groups(lines: Sequence[object]) -> tuple[tuple[object, ...], ...]:
    """Stable two-column display groups, with the largest package first."""

    ordered = sorted(
        lines,
        key=lambda line: (
            -float(getattr(line, "package_grams", 0.0)),
            str(getattr(line, "basket_item_id", "") or ""),
            str(getattr(line, "package_id", "") or ""),
            str(getattr(line, "package_label", "")),
        ),
    )
    return tuple(
        tuple(ordered[index:index + 2])
        for index in range(0, len(ordered), 2)
    )


def quantity_rich_text(label: str, pantry_suffix: str | None = None) -> ft.Text:
    spans = [
        ft.TextSpan(
            label,
            style=ft.TextStyle(
                size=12.5,
                weight=ft.FontWeight.W_600,
                color=theme.TEXT,
            ),
        )
    ]
    if pantry_suffix:
        spans.append(
            ft.TextSpan(
                f"  {pantry_suffix}",
                style=ft.TextStyle(
                    size=11,
                    color=theme.PRIMARY,
                    italic=True,
                ),
            )
        )
    return ft.Text(spans=spans)


def package_columns_control(
    columns: Sequence[ft.Container],
    *,
    key: str,
) -> ft.Container:
    """One shared white frame containing an equal-width two-column group."""

    controls: list[ft.Control] = []
    for index, column in enumerate(columns):
        if index:
            controls.append(ft.VerticalDivider(width=1, color=theme.BORDER))
        column.expand = True
        controls.append(column)
    if len(columns) == 1:
        controls.extend(
            [
                ft.VerticalDivider(width=1, color=theme.BORDER),
                ft.Container(expand=True),
            ]
        )
    return ft.Container(
        key=key,
        bgcolor=theme.SURFACE,
        border=ft.Border.all(1, theme.BORDER),
        border_radius=theme.RADIUS_SM,
        content=ft.Row(
            controls,
            spacing=0,
            intrinsic_height=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


def aligned_food_card_grid(cards: Sequence[ft.Container]) -> ft.ResponsiveRow:
    """Responsive grid that preserves each card's compact content height."""

    controls = list(cards)
    for food_card in controls:
        food_card.width = None
        food_card.col = {
            ft.ResponsiveRowBreakpoint.XS: 12,
            ft.ResponsiveRowBreakpoint.LG: 6,
        }
    return ft.ResponsiveRow(
        controls,
        columns=12,
        spacing=12,
        run_spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )


def purchase_input_for_line(food: Food, line) -> PurchaseInput:
    """Preserve one package column's saved identity in its purchase event."""

    if line.basket_item_id is None or line.package_id is None:
        raise ValueError("a direct purchase requires a saved basket/package link")
    return PurchaseInput(
        event_id=new_purchase_event_id(),
        food_id=food.id,
        raw_name=food.name,
        package_label=line.package_label,
        grams=line.package_grams * line.count,
        quantity=line.count,
        estimated_line_cost=line.est_cost,
        apply_to_plan=True,
        group_id=new_purchase_event_id(),
        origin=ORIGIN_DIRECT_BUTTON,
        basket_item_id=line.basket_item_id,
        package_id=line.package_id,
    )


def live_line_control_key(food_id: str, line) -> str:
    """Stable key for one live package/offer child row."""

    return line.basket_item_id or f"unplanned:{food_id}:{line.package_id or line.package_label}"


def build_gap_rows(consumed: Nutrients, available: Nutrients, targets: Nutrients) -> list[GapRow]:
    """``available`` is the honest three-source sum (purchases + pantry grams
    the plan uses + scheduled prepared leftovers) — no clamping that would
    paper over an accounting mismatch."""
    return [
        GapRow(
            name,
            consumed.get(name),
            available.get(name),
            targets.get(name),
        )
        for name in Nutrients.NAMES
    ]


def pantry_coverage_note(food: Food, from_pantry: float, unit=None) -> str | None:
    if from_pantry <= GRAM_EPSILON:
        return None
    amount = (
        format_grams(food, from_pantry, unit)
        if unit is not None else carryover_amount_label(food, from_pantry)
    )
    return f"{amount} covered by pantry"


def shopping_quantity_label(food: Food, allocation: FoodAllocation) -> str:
    """Package need with any pantry coverage expressed in the same unit."""

    ordered = sorted(
        allocation.to_buy, key=lambda line: line.package_grams, reverse=True
    )
    label = " + ".join(
        package_quantity_label(line.package_label, line.count)
        for line in ordered
    )
    suffix = (
        pantry_quantity_suffix(food, allocation.from_pantry, ordered[0].package_label)
        if ordered else None
    )
    if suffix:
        label = f"{label} {suffix}" if label else suffix
    return label


def _empty_state(on_go_to_start: Callable[[], None]) -> ft.Container:
    button = primary_button("Go to Start", icon=ft.Icons.ROCKET_LAUNCH_OUTLINED)
    button.on_click = lambda e: on_go_to_start()
    return card(
        ft.Column(
            [
                ft.Icon(ft.Icons.SHOPPING_BASKET_OUTLINED, size=48, color=theme.TEXT_MUTED),
                ft.Text("No plan yet", size=16, weight=ft.FontWeight.W_600, color=theme.TEXT),
                muted_text(
                    "Set an estimated basket budget cap and dates on the Start tab, "
                    "and RightMeal builds "
                    "your basket and daily meals."
                ),
                ft.Container(height=4),
                button,
            ],
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=40,
    )


def _pantry_use_block(
    state: AppState,
    entries: list[tuple[str, float]],
    title: str,
    subtitle: str,
) -> ft.Column:
    """Rows for pantry grams sourcing this plan — live allocation on the
    active plan, the frozen snapshot on a historical one."""
    foods_by_id = state.foods_by_id
    rows: list[ft.Control] = [
        ft.Text(title, size=13.5, weight=ft.FontWeight.W_600, color=theme.TEXT),
        muted_text(subtitle, size=12.5),
    ]
    for food_id, grams in entries:
        food = foods_by_id.get(food_id)
        if food is None:
            continue
        rows.append(
            ft.Row(
                [
                    food_avatar(food, size=24, image_src=state.image_src_for(food)),
                    ft.Text(food.name, size=12.5, color=theme.TEXT, expand=True,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    muted_text(
                        "Use "
                        + format_grams(
                            food,
                            grams,
                            preferred_package_unit(
                                food, state.saved_plan, state.purchase_log
                            ),
                        )
                        + " from pantry",
                        size=12,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
    return ft.Column(rows, spacing=8)


def _basket_section(page: ft.Page, plan: SavedPlan, state: AppState) -> ft.Container:
    """Render planned offer rows and purchase-event history as separate facts."""

    foods_by_id = state.foods_by_id
    live = not is_historical(plan)
    saved_by_id = {item.basket_item_id: item for item in plan.basket}
    grouped: dict[str, list[SavedBasketItem]] = {}
    for item in plan.basket:
        if item.food_id in foods_by_id:
            grouped.setdefault(item.food_id, []).append(item)

    body_column = ft.Column(spacing=12)
    ui_state = {"purchased_expanded": False}
    receipt_picker = ensure_file_picker(page)

    def restore(snapshot) -> None:
        state.pantry.items.clear()
        state.pantry.items.update(snapshot[0])
        plan.purchased.clear()
        plan.purchased.update(snapshot[1])
        plan.purchased_baseline.clear()
        plan.purchased_baseline.update(snapshot[2])
        state.purchase_log[:] = snapshot[3]

    def commit(mutate: Callable[[], None]) -> bool:
        snapshot = (
            dict(state.pantry.items),
            dict(plan.purchased),
            dict(plan.purchased_baseline),
            list(state.purchase_log),
        )
        try:
            mutate()
            state.persist(plan=plan, pantry=state.pantry, purchases=state.purchase_log)
            return True
        except Exception:  # noqa: BLE001 - roll memory back with the disk transaction
            restore(snapshot)
            page.show_dialog(ft.SnackBar(ft.Text("Couldn't save — nothing was changed.")))
            return False

    def purchases_paused() -> bool:
        if not state.purchase_log_error:
            return False
        page.show_dialog(
            ft.SnackBar(
                ft.Text(
                    "Purchase history could not be read — purchasing is paused to "
                    "protect your data."
                )
            )
        )
        return True

    def on_receipt_committed() -> None:
        rebuild()
        page.update()

    async def on_receipt_click(e) -> None:
        await run_receipt_flow(page, state, receipt_picker, on_receipt_committed)

    receipt_button = ft.FilledTonalButton(
        content="Import receipt photos",
        icon=ft.Icons.RECEIPT_LONG_OUTLINED,
        on_click=on_receipt_click,
        tooltip="Auto-import reliable receipt foods; review only uncertain items",
    )

    def undo_handler(group_id: str) -> Callable:
        def handler(e) -> None:
            if purchases_paused():
                return
            ok, message = can_void_group(plan, state.pantry, state.purchase_log, group_id)
            if not ok:
                page.show_dialog(ft.SnackBar(ft.Text(message or "This group cannot be undone.")))
                rebuild()
                page.update()
                return
            if commit(
                lambda: void_purchase_group(
                    plan, state.pantry, state.purchase_log, group_id
                )
            ):
                rebuild()
                page.update()

        return handler

    def direct_purchase_handler(food: Food, line) -> Callable:
        def handler(e) -> None:
            if purchases_paused():
                return
            if line.basket_item_id is None or line.package_id is None:
                page.show_dialog(
                    ft.SnackBar(
                        ft.Text(
                            "This pantry gap is not linked to a saved offer. "
                            "Import a receipt to record it."
                        )
                    )
                )
                return
            purchase_input = purchase_input_for_line(food, line)
            # The purchase service validates the basket/package/food/grams
            # linkage before it mutates either pantry or history.
            if commit(
                lambda: record_purchase_events(
                    plan, state.pantry, state.purchase_log, [purchase_input]
                )
            ):
                rebuild()
                page.update()

        return handler

    def details_for(item: SavedBasketItem | None, line=None) -> list[tuple[str, str]]:
        source_value = item.source if item is not None else getattr(line, "source", "")
        try:
            source_label = PRICE_SOURCE_LABELS[PriceSource(source_value)]
        except (KeyError, ValueError):
            source_label = "Planning estimate"
        if item is None:
            return [
                ("Package", getattr(line, "package_label", "")),
                ("Store", getattr(line, "store", "") or "Seed data"),
                ("Price source", source_label),
                ("Offer link", "Not part of the frozen basket"),
            ]
        return [
            ("Package", saved_item_quantity_label(foods_by_id[item.food_id], item)),
            ("Matched product", item.matched_product_name),
            ("Store", item.store),
            ("Price source", source_label),
            ("Match confidence", f"{item.confidence:.0%}"),
            ("Why this match", item.match_reason),
        ]

    def info_handler(food: Food, details: list[tuple[str, str]]) -> Callable:
        def handler(e) -> None:
            rows = [
                ft.Row(
                    [
                        ft.Container(muted_text(name, size=12), width=130),
                        ft.Text(value or "—", size=12.5, color=theme.TEXT, expand=True),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                )
                for name, value in details
            ]
            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text(food.name, size=15, weight=ft.FontWeight.W_600),
                    content=ft.Container(
                        width=400,
                        content=ft.Column(rows, spacing=6, tight=True),
                    ),
                    actions=[
                        ft.TextButton(
                            content="Close", on_click=lambda event: page.pop_dialog()
                        )
                    ],
                )
            )

        return handler

    def package_row(
        food: Food,
        *,
        key: str,
        label: str,
        package_grams: float,
        count: int,
        cost: float,
        details: list[tuple[str, str]],
        line=None,
        basket_item_id: str | None = None,
        package_id: str | None = None,
        pantry_suffix: str | None = None,
        column_layout: bool = False,
        bordered: bool = True,
    ) -> ft.Container:
        physical = (
            format_grams(food, package_grams)
            if package_grams > 0
            else "Weight unavailable"
        )
        quantity = quantity_rich_text(label, pantry_suffix)
        physical_note = muted_text(
            (
                f"{physical} · {count} pkg" + ("s" if count != 1 else "")
                if column_layout
                else f"{physical} per package · {count} package"
                + ("s" if count != 1 else "")
            ),
            size=11 if column_layout else 11.5,
        )
        price = ft.Text(
            f"${cost:.2f}", size=13, weight=ft.FontWeight.W_700, color=theme.TEXT
        )
        info = ft.IconButton(
            icon=ft.Icons.INFO_OUTLINE,
            icon_size=16,
            icon_color=theme.TEXT_MUTED,
            width=24,
            height=24,
            padding=1,
            tooltip="This package offer's price details",
            on_click=info_handler(food, details),
        )
        button: ft.FilledTonalButton | None = None
        if live and line is not None:
            button = ft.FilledTonalButton(
                content="Purchased",
                icon=None if column_layout else ft.Icons.SHOPPING_CART_CHECKOUT,
                height=28,
                style=ft.ButtonStyle(
                    padding=ft.Padding.symmetric(horizontal=8),
                    icon_size=16,
                    text_style=ft.TextStyle(size=12.5),
                    visual_density=ft.VisualDensity.COMPACT,
                ),
                key=f"purchase:{key}",
                on_click=direct_purchase_handler(food, line),
            )
            if line.basket_item_id is None or line.package_id is None:
                button.disabled = True
                button.tooltip = "This row has no saved package-offer link."
        controls: list[ft.Control] = [
            ft.Column(
                [quantity, physical_note],
                spacing=0 if column_layout else 2,
                expand=True,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            info,
            price,
        ]
        if button is not None:
            # Keep the price immediately to the left of the purchase action.
            controls.append(button)
        content: ft.Control = ft.Row(
            controls,
            spacing=4 if column_layout else 8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        return ft.Container(
            key=key,
            data={
                "basket_item_id": (
                    basket_item_id
                    or getattr(line, "basket_item_id", None)
                    or key
                ),
                "package_id": package_id or getattr(line, "package_id", None),
            },
            content=content,
            height=PACKAGE_ROW_HEIGHT,
            expand=True if column_layout else None,
            padding=ft.Padding.symmetric(
                horizontal=8 if column_layout else 10,
                vertical=4,
            ),
            bgcolor=theme.SURFACE,
            border=ft.Border.all(1, theme.BORDER) if bordered else None,
            border_radius=theme.RADIUS_SM,
        )

    def food_card(
        food: Food,
        rows: list[ft.Control],
    ) -> ft.Container:
        height = (
            SINGLE_PACKAGE_CARD_HEIGHT
            + max(0, len(rows) - 1) * EXTRA_PACKAGE_GROUP_HEIGHT
        )
        return ft.Container(
            key=f"food-card:{food.id}",
            width=520,
            height=height,
            alignment=ft.Alignment.TOP_LEFT,
            padding=12,
            bgcolor=theme.SURFACE_TINT,
            border=ft.Border.all(1, theme.BORDER),
            border_radius=theme.RADIUS_SM,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            food_avatar(food, size=32, image_src=state.image_src_for(food)),
                            ft.Text(
                                food.name,
                                size=13.5,
                                weight=ft.FontWeight.W_600,
                                color=theme.TEXT,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                    ),
                    *rows,
                ],
                spacing=8,
            ),
        )

    def live_food_card(food_id: str, allocation: FoodAllocation) -> ft.Container:
        food = foods_by_id[food_id]
        rows: list[ft.Control] = []
        groups = package_line_groups(allocation.to_buy)
        multi_package = len(allocation.to_buy) > 1
        pantry_attached = False
        for group_index, group in enumerate(groups):
            columns: list[ft.Container] = []
            child_keys: list[str] = []
            for line in group:
                key = live_line_control_key(food_id, line)
                child_keys.append(key)
                suffix = None
                if not pantry_attached:
                    suffix = pantry_quantity_suffix(
                        food,
                        allocation.from_pantry,
                        line.package_label,
                    )
                    pantry_attached = suffix is not None
                saved = saved_by_id.get(line.basket_item_id or "")
                columns.append(
                    package_row(
                        food,
                        key=key,
                        label=package_quantity_label(line.package_label, line.count),
                        package_grams=line.package_grams,
                        count=line.count,
                        cost=line.est_cost,
                        details=details_for(saved, line),
                        line=line,
                        pantry_suffix=suffix,
                        column_layout=multi_package,
                        bordered=not multi_package,
                    )
                )
            if multi_package:
                rows.append(
                    package_columns_control(
                        columns,
                        key=f"package-group:{food_id}:{group_index}:" + ":".join(child_keys),
                    )
                )
            else:
                rows.extend(columns)
        return food_card(food, rows)

    def snapshot_food_card(food_id: str, items: list[SavedBasketItem]) -> ft.Container:
        food = foods_by_id[food_id]
        # Historical price/weight/details stay frozen. Only their visual
        # grouping changes; current pantry state is never consulted here.
        groups = package_line_groups(items)
        multi_package = len(items) > 1
        rows: list[ft.Control] = []
        for group_index, group in enumerate(groups):
            columns = [
                package_row(
                    food,
                    key=item.basket_item_id,
                    label=package_quantity_label(item.package_label, item.count),
                    package_grams=item.package_grams,
                    count=item.count,
                    cost=item.cost,
                    details=details_for(item),
                    basket_item_id=item.basket_item_id,
                    package_id=item.package_id,
                    column_layout=multi_package,
                    bordered=not multi_package,
                )
                for item in group
            ]
            if multi_package:
                rows.append(
                    package_columns_control(
                        columns,
                        key=f"snapshot-package-group:{food_id}:{group_index}:"
                        + ":".join(item.basket_item_id for item in group),
                    )
                )
            else:
                rows.extend(columns)
        return food_card(food, rows)

    def purchase_record_row(record: PurchaseRecord) -> ft.Row:
        food = foods_by_id.get(record.food_id)
        name = food.name if food is not None else (record.raw_name or record.food_id)
        amount = (
            format_grams(food, record.grams)
            if food is not None
            else f"{record.grams:g} g"
        )
        price = "Price not recorded"
        if record.line_total is not None:
            price = f"${record.line_total:.2f} confirmed"
        elif record.estimated_line_cost is not None:
            price = f"${record.estimated_line_cost:.2f} estimated"
        return ft.Row(
            [
                ft.Text(name, size=12.5, color=theme.TEXT, expand=True),
                muted_text(
                    f"{record.package_label or 'Package not recorded'} · +{amount}",
                    size=11.5,
                ),
                muted_text(price, size=11.5),
            ],
            spacing=10,
        )

    def purchase_group_card(
        group_id: str, records: tuple[PurchaseRecord, ...]
    ) -> ft.Container:
        undo = ft.TextButton(content="Undo whole group")
        if not live:
            undo.disabled = True
            undo.tooltip = "This plan has ended — its purchase history is frozen."
        elif not records[0].group_id:
            undo.disabled = True
            undo.tooltip = "This legacy event has no reversible group."
        else:
            ok, reason = can_void_group(plan, state.pantry, state.purchase_log, group_id)
            if ok:
                undo.on_click = undo_handler(group_id)
                undo.tooltip = "Undo this whole purchase group."
            else:
                undo.disabled = True
                undo.tooltip = reason
        return ft.Container(
            key=f"purchase-group:{group_id}",
            padding=10,
            border=ft.Border.all(1, theme.BORDER),
            border_radius=theme.RADIUS_SM,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text(
                                        records[0].origin.replace("_", " ").title(),
                                        size=12.5,
                                        weight=ft.FontWeight.W_600,
                                        color=theme.TEXT,
                                    ),
                                    muted_text(
                                        records[-1].purchased_at.replace("T", " "), size=11
                                    ),
                                ],
                                spacing=1,
                                expand=True,
                            ),
                            undo,
                        ]
                    ),
                    *[purchase_record_row(record) for record in records],
                ],
                spacing=6,
            ),
        )

    def on_purchased_toggle(e) -> None:
        ui_state["purchased_expanded"] = e.data == "true"

    def rebuild() -> None:
        body: list[ft.Control] = []
        if plan.plan_kind is PlanKind.PARTIAL_FOOD_COVERAGE:
            body.append(partial_plan_warning_banner(compact=True))
        if state.profile is not None and plan.profile_stale(state.profile):
            body.append(stale_plan_warning_banner(compact=True))
        if live:
            body.append(ft.Row([receipt_button]))

        purchase_groups = plan_purchase_groups(plan, state.purchase_log)
        if live:
            allocations = allocate_sources(plan, state.pantry, foods_by_id)
            pantry_only = [
                (food_id, allocation.from_pantry)
                for food_id, allocation in sorted(allocations.items())
                if allocation.from_pantry > GRAM_EPSILON
                and not allocation.to_buy
            ]
            if pantry_only:
                body.append(
                    _pantry_use_block(
                        state,
                        pantry_only,
                        "Available for remaining meals",
                        "Already at home — reserved for this plan's remaining meals.",
                    )
                )
                body.append(ft.Divider(height=1, color=theme.BORDER))
            to_buy = [
                food_id
                for food_id in grouped
                if allocations.get(food_id) and allocations[food_id].to_buy
            ]
            to_buy.extend(
                food_id
                for food_id in sorted(allocations)
                if food_id not in grouped and allocations[food_id].to_buy
            )
            if to_buy or pantry_only or purchase_groups:
                body.append(
                    ft.Text(
                        "Need to buy",
                        size=13.5,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT,
                    )
                )
            if to_buy:
                body.append(
                    aligned_food_card_grid(
                        [
                            live_food_card(food_id, allocations[food_id])
                            for food_id in to_buy
                        ]
                    )
                )
            elif pantry_only:
                body.append(muted_text("Your pantry covers the remaining plan.", size=12.5))
        else:
            if plan.pantry_used:
                body.append(
                    _pantry_use_block(
                        state,
                        sorted(plan.pantry_used.items()),
                        "Used from pantry",
                        "What this plan drew from stock you already had.",
                    )
                )
                body.append(ft.Divider(height=1, color=theme.BORDER))
            if grouped:
                body.append(
                    ft.Text(
                        "Planned basket (plan ended)",
                        size=13.5,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT,
                    )
                )
                body.append(
                    aligned_food_card_grid(
                        [
                            snapshot_food_card(food_id, grouped[food_id])
                            for food_id in grouped
                        ]
                    )
                )

        if purchase_groups:
            group_cards = [
                purchase_group_card(group_id, records)
                for group_id, records in reversed(purchase_groups)
            ]
            body.append(
                ft.ExpansionTile(
                    title=ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.CHECK_CIRCLE_OUTLINE,
                                size=16,
                                color=theme.PRIMARY,
                            ),
                            ft.Text(
                                f"Purchased ({len(purchase_groups)} groups)",
                                size=13.5,
                                weight=ft.FontWeight.W_600,
                                color=theme.TEXT,
                            ),
                        ],
                        spacing=8,
                    ),
                    expanded=ui_state["purchased_expanded"],
                    on_change=on_purchased_toggle,
                    shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                    collapsed_shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                    icon_color=theme.TEXT_MUTED,
                    collapsed_icon_color=theme.TEXT_MUTED,
                    tile_padding=ft.Padding.symmetric(horizontal=4),
                    controls_padding=ft.Padding.only(left=4, right=4, bottom=8),
                    controls=[ft.Column(group_cards, spacing=8)],
                )
            )
        if plan.staples:
            body.append(ft.Divider(height=1, color=theme.BORDER))
            body.append(
                ft.Text(
                    "Pantry staples to check",
                    size=13.5,
                    weight=ft.FontWeight.W_600,
                    color=theme.TEXT,
                )
            )
            body.append(
                muted_text(
                    "Common seasonings the recipes use — check you have them (not priced): "
                    + ", ".join(plan.staples)
                    + ".",
                    size=12.5,
                )
            )
        body_column.controls = body

    rebuild()
    return collapsible_section(
        "Your basket",
        body_column,
        icon=ft.Icons.SHOPPING_BASKET_OUTLINED,
        icon_tooltip="The full supermarket shopping list for this plan",
        source_note=(
            "Each package row keeps its own offer and source. "
            + ESTIMATED_CAP_DISCLAIMER
        ),
        subtitle=f"Shopping and pantry sourcing for the {plan.horizon_days}-day plan",
        expanded=True,
    )


def _explanation_section(explanation: Explanation) -> ft.Container:
    children: list[ft.Control] = [ft.Text(explanation.summary, size=13.5, color=theme.TEXT)]
    if explanation.item_reasons:
        children.append(
            ft.ExpansionTile(
                title=ft.Text("Item by item", size=13.5, weight=ft.FontWeight.W_500, color=theme.TEXT),
                shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                collapsed_shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                icon_color=theme.TEXT_MUTED,
                collapsed_icon_color=theme.TEXT_MUTED,
                tile_padding=ft.Padding.symmetric(horizontal=4),
                controls=[
                    ft.Container(
                        content=ft.Text(f"{name}: {reason}", size=13, color=theme.TEXT),
                        padding=ft.Padding.symmetric(horizontal=16, vertical=4),
                    )
                    for name, reason in explanation.item_reasons.items()
                ],
            )
        )
    for text in (
        explanation.budget_tradeoffs,
        explanation.food_group_coverage,
        explanation.life_impact,
    ):
        if text:
            children.append(ft.Text(text, size=13, color=theme.TEXT))
    if explanation.generated_by == "openai":
        tag = pill("AI-assisted", theme.PRIMARY_TINT, theme.PRIMARY_DARK)
    else:
        tag = pill("Generated locally", theme.SURFACE_TINT, theme.TEXT_MUTED)
    children.append(ft.Row([tag]))
    return collapsible_section(
        "Why this basket?",
        *children,
        icon=ft.Icons.LIGHTBULB_OUTLINE,
        icon_tooltip="How the budget and nutrition targets shaped these picks",
        expanded=False,
    )


def _plan_sidebar(
    nav_items: list[tuple[str, str, str]], content_column: ft.Column
) -> ft.Container:
    """A translucent, icon-only floating rail that jumps the content column
    to a section. Tooltips carry the labels since there's no room for text."""

    def nav_button(icon: str, label: str, scroll_key: str) -> ft.Container:
        async def go(e):
            await content_column.scroll_to(scroll_key=scroll_key, duration=300)

        return ft.Container(
            content=ft.Icon(icon, size=18, color=theme.TEXT_MUTED),
            width=40,
            height=40,
            alignment=ft.Alignment.CENTER,
            border_radius=999,
            ink=True,
            tooltip=label,
            on_click=go,
        )

    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=6, vertical=10),
        border_radius=999,
        bgcolor=ft.Colors.with_opacity(0.6, theme.SURFACE),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.5, theme.BORDER)),
        shadow=ft.BoxShadow(
            spread_radius=0,
            blur_radius=20,
            color=ft.Colors.with_opacity(0.10, "#12301C"),
            offset=ft.Offset(0, 6),
        ),
        content=ft.Column(
            [nav_button(icon, label, key) for icon, label, key in nav_items],
            spacing=4,
            tight=True,
        ),
    )


def build_planning_view(
    page: ft.Page,
    state: AppState,
    on_go_to_start: Callable[[], None],
    initial_date: date | None = None,
) -> ft.Control:
    plan = state.saved_plan
    if plan is None:
        return _empty_state(on_go_to_start)

    profile = state.profile
    foods_by_id = state.foods_by_id
    # Live plans re-derive ingredient sourcing from the current pantry;
    # historical plans render exclusively from their frozen snapshot.
    allocations = (
        allocate_sources(plan, state.pantry, foods_by_id)
        if not is_historical(plan)
        else None
    )
    controls: list[ft.Control] = []
    nav_items: list[tuple[str, str, str]] = []

    if plan.plan_kind is PlanKind.PARTIAL_FOOD_COVERAGE:
        controls.append(partial_plan_warning_banner())
    if profile is not None and plan.profile_stale(profile):
        controls.append(stale_plan_warning_banner())

    strip = warnings_strip(
        plan.nutrition_feasible,
        plan.budget_status,
        plan.relaxed_constraints,
        plan.dominance_flags,
    )
    if strip is not None:
        controls.append(strip)

    if plan.basket or plan.pantry_used:
        if plan.basket:
            # Provenance is offer-specific.  Two package offers for one food
            # are two source facts even though distinct-food metrics count one.
            source_mix = basket_source_mix(plan.basket)
            if allocations is not None:
                est_purchased = purchased_value(plan, state.purchase_log, plan.basket)
                still_needed = dynamic_open_cost(allocations)
                controls.append(
                    budget_bar(
                        est_purchased + still_needed, plan.budget, plan.horizon_days,
                        source_mix_caption(source_mix) + " " + ESTIMATED_CAP_DISCLAIMER,
                        status_line=(
                            f"Estimated purchased: ${est_purchased:.2f} · "
                            f"Estimated still needed: ${still_needed:.2f} · "
                            f"Original estimate: ${plan.total_cost:.2f}"
                        ),
                    )
                )
            else:
                controls.append(
                    budget_bar(
                        plan.total_cost, plan.budget, plan.horizon_days,
                        source_mix_caption(source_mix) + " " + ESTIMATED_CAP_DISCLAIMER,
                    )
                )

        basket_section = _basket_section(page, plan, state)
        basket_section.key = ft.ScrollKey("section-basket")
        controls.append(basket_section)
        nav_items.append((ft.Icons.SHOPPING_BASKET_OUTLINED, "Basket", "section-basket"))

        meals_section = daily_meals_section(
            page,
            state,
            plan,
            state.nutrition.household_daily_targets(profile),
            initial_date=initial_date,
        )
        meals_section.key = ft.ScrollKey("section-meals")
        controls.append(meals_section)
        nav_items.append((ft.Icons.RESTAURANT_MENU, "Daily meals", "section-meals"))

        covered_groups: set[FoodGroup] = {
            foods_by_id[food_id].food_group
            for food_id in (
                {item.food_id for item in plan.basket} | set(plan.pantry_used)
            )
            if food_id in foods_by_id
        }
        targets = state.nutrition.household_targets(profile, plan.horizon_days)
        if allocations is not None:
            # Covered = what current stock + completed cooking secure of the
            # plan's meals (live allocation), plus scheduled ready-meals —
            # they draw zero raw ingredients, so there is no overlap. Batch
            # leftovers are already inside the allocation (2× dinner draw).
            available = covered_nutrients(allocations, foods_by_id).plus(
                prepared_meal_totals(plan.meal_plan)
            )
        else:
            # Historical snapshot: the frozen three-source sum.
            available = plan.purchased_totals.plus(
                pantry_used_totals(plan.pantry_used, foods_by_id)
            ).plus(prepared_meal_totals(plan.meal_plan))
        gap_rows = build_gap_rows(plan.meal_plan.consumed_totals, available, targets)
        nutrition_section = nutrition_card(gap_rows, covered_groups, plan.horizon_days)
        nutrition_section.key = ft.ScrollKey("section-nutrition")
        controls.append(nutrition_section)
        nav_items.append((ft.Icons.MONITOR_HEART, "Nutrition", "section-nutrition"))

    if plan.explanation is not None:
        why_section = _explanation_section(plan.explanation)
        why_section.key = ft.ScrollKey("section-why")
        controls.append(why_section)
        nav_items.append((ft.Icons.LIGHTBULB_OUTLINE, "Why this basket?", "section-why"))

    content_column = ft.Column(
        controls,
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
    if not nav_items:
        return content_column
    return ft.Stack(
        [
            ft.Container(
                content=content_column,
                padding=ft.Padding.only(left=56),
                left=0,
                top=0,
                right=0,
                bottom=0,
            ),
            ft.Container(content=_plan_sidebar(nav_items, content_column), left=4, top=4),
        ],
        expand=True,
    )
