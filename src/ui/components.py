"""Shared UI building blocks."""

from __future__ import annotations

from typing import Callable, NamedTuple

import flet as ft

import theme
from models import (
    FOOD_GROUP_LABELS,
    BudgetStatus,
    Food,
    FoodGroup,
    Nutrients,
    PriceSource,
)

SOURCE_SHORT_NAMES = {
    PriceSource.KROGER_REAL_PRICE: "Kroger",
    PriceSource.INSTACART_NUMERIC_PRICE: "Instacart",
    PriceSource.BLS_REGIONAL_AVERAGE: "BLS",
    PriceSource.SEED_ESTIMATE: "seed",
}


class NutrientStyle(NamedTuple):
    icon: str
    color: str
    blurb: str


NUTRIENT_STYLES: dict[str, NutrientStyle] = {
    "calories_kcal": NutrientStyle(
        ft.Icons.LOCAL_FIRE_DEPARTMENT, "#F97316", "Energy that fuels everything your body does."
    ),
    "protein_g": NutrientStyle(
        ft.Icons.FITNESS_CENTER, "#E11D48", "Builds and repairs muscles and tissues."
    ),
    "fiber_g": NutrientStyle(
        ft.Icons.GRASS, "#65A30D", "Keeps digestion running and helps you feel full."
    ),
    "calcium_mg": NutrientStyle(
        ft.Icons.ACCESSIBILITY_NEW, "#0284C7", "Keeps bones and teeth strong."
    ),
    "iron_mg": NutrientStyle(
        ft.Icons.BLOODTYPE, "#B91C1C", "Carries oxygen through your blood."
    ),
    "potassium_mg": NutrientStyle(
        ft.Icons.ELECTRIC_BOLT, "#4F46E5", "Helps muscles, nerves, and blood pressure stay steady."
    ),
    "vitamin_a_mcg_rae": NutrientStyle(
        ft.Icons.VISIBILITY, "#7C3AED", "Supports eyesight and a healthy immune system."
    ),
    "vitamin_c_mg": NutrientStyle(
        ft.Icons.HEALING, "#0D9488", "Helps heal wounds and absorb iron from plants."
    ),
    "vitamin_d_mcg": NutrientStyle(
        ft.Icons.WB_SUNNY, "#EAB308", "Helps the body absorb calcium for strong bones."
    ),
    "folate_mcg_dfe": NutrientStyle(
        ft.Icons.SPA, "#059669", "Helps the body build new, healthy cells."
    ),
    "magnesium_mg": NutrientStyle(
        ft.Icons.SELF_IMPROVEMENT, "#64748B", "Supports muscles, nerves, and steady energy."
    ),
    "zinc_mg": NutrientStyle(
        ft.Icons.SHIELD, "#C026D3", "Helps the immune system fight off germs."
    ),
}

assert set(NUTRIENT_STYLES) == set(Nutrients.NAMES), "NUTRIENT_STYLES out of sync"


def nutrient_unit(name: str) -> str:
    """Display unit derived from the nutrient field-name suffix."""
    if name.endswith("_kcal"):
        return "kcal"
    if name.endswith(("_mcg", "_mcg_rae", "_mcg_dfe")):
        return "mcg"
    if name.endswith("_mg"):
        return "mg"
    return "g"


def nutrient_amount(value: float, name: str) -> str:
    """e.g. '31,500 kcal', '840 mg'."""
    return f"{value:,.0f} {nutrient_unit(name)}"


def nutrient_fraction(value: float, target: float, name: str) -> str:
    """e.g. '33,357 / 42,000 kcal' — amount over target, unit once."""
    return f"{value:,.0f} / {target:,.0f} {nutrient_unit(name)}"


class GroupAvatarStyle(NamedTuple):
    icon: str
    bg: str
    ink: str


GROUP_AVATAR_STYLES: dict[FoodGroup, GroupAvatarStyle] = {
    FoodGroup.GRAINS_STARCHY: GroupAvatarStyle(ft.Icons.BAKERY_DINING, "#FEF3C7", "#92400E"),
    FoodGroup.PROTEIN: GroupAvatarStyle(ft.Icons.EGG_ALT, "#FEE2E2", "#B91C1C"),
    FoodGroup.VEGETABLES: GroupAvatarStyle(ft.Icons.ECO, "#DCFCE7", "#166534"),
    FoodGroup.FRUITS: GroupAvatarStyle(ft.Icons.APPLE, "#FFEDD5", "#C2410C"),
    FoodGroup.DAIRY_FORTIFIED_ALT: GroupAvatarStyle(ft.Icons.LOCAL_DRINK, "#E0F2FE", "#075985"),
    FoodGroup.HEALTHY_FATS: GroupAvatarStyle(ft.Icons.WATER_DROP, "#FEF9C3", "#854D0E"),
}


def card(
    *controls: ft.Control,
    spacing: int = 12,
    padding: int | ft.Padding = 20,
    bgcolor: str = theme.SURFACE,
    border_color: str = theme.BORDER,
) -> ft.Container:
    """A white card with a hairline border and a soft shadow."""
    return ft.Container(
        content=ft.Column(list(controls), spacing=spacing),
        bgcolor=bgcolor,
        border=ft.Border.all(1, border_color),
        border_radius=theme.RADIUS,
        padding=padding,
        shadow=ft.BoxShadow(
            spread_radius=0,
            blur_radius=12,
            color=ft.Colors.with_opacity(0.04, "#12301C"),
            offset=ft.Offset(0, 3),
        ),
    )


def section_card(title: str | None, *controls: ft.Control, subtitle: str | None = None) -> ft.Container:
    """A card with an optional heading and subheading."""
    children: list[ft.Control] = []
    if title:
        children.append(ft.Text(title, size=15, weight=ft.FontWeight.W_600, color=theme.TEXT))
    if subtitle:
        children.append(muted_text(subtitle, size=13))
    children.extend(controls)
    return card(*children)


def collapsible_section(
    title: str,
    *controls: ft.Control,
    icon: str | None = None,
    icon_tooltip: str | None = None,
    subtitle: str | None = None,
    expanded: bool = True,
    source_note: str | None = None,
) -> ft.Container:
    """A card whose body folds away behind a styled ExpansionTile header."""
    title_row: list[ft.Control] = []
    if icon:
        title_row.append(ft.Icon(icon, size=18, color=theme.PRIMARY, tooltip=icon_tooltip))
    title_row.append(ft.Text(title, size=15, weight=ft.FontWeight.W_600, color=theme.TEXT))
    if source_note:
        title_row_widget: ft.Control = ft.Row(
            [
                ft.Row(title_row, spacing=8),
                ft.Icon(
                    ft.Icons.INFO_OUTLINE, size=15, color=theme.TEXT_MUTED, tooltip=source_note
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )
    else:
        title_row_widget = ft.Row(title_row, spacing=8)
    tile = ft.ExpansionTile(
        title=title_row_widget,
        subtitle=muted_text(subtitle, size=12.5) if subtitle else None,
        expanded=expanded,
        maintain_state=True,
        shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
        collapsed_shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
        icon_color=theme.TEXT_MUTED,
        collapsed_icon_color=theme.TEXT_MUTED,
        tile_padding=ft.Padding.symmetric(horizontal=4),
        controls_padding=ft.Padding.only(left=4, right=4, bottom=8),
        controls=[ft.Column(list(controls), spacing=12)],
    )
    return card(tile, padding=ft.Padding.symmetric(horizontal=12, vertical=6))


def muted_text(value: str, size: int = 13) -> ft.Text:
    return ft.Text(value, size=size, color=theme.TEXT_MUTED)


def pill(label: str, bg: str, ink: str, tooltip: str | None = None) -> ft.Container:
    """A small rounded tag with a tinted background."""
    return ft.Container(
        content=ft.Text(label, size=11.5, weight=ft.FontWeight.W_600, color=ink),
        bgcolor=bg,
        border_radius=999,
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
        tooltip=tooltip,
    )


def primary_button(label: str, icon: str | None = None) -> ft.FilledButton:
    return ft.FilledButton(
        content=label,
        icon=icon,
        bgcolor=theme.PRIMARY,
        color=theme.SURFACE,
        height=44,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
            padding=ft.Padding.symmetric(horizontal=20),
        ),
    )


def style_field(field: ft.TextField | ft.Dropdown) -> None:
    """Apply the shared input look: rounded hairline border, green focus."""
    field.border_radius = theme.RADIUS_SM
    field.border_color = theme.BORDER
    field.focused_border_color = theme.PRIMARY


def food_avatar(food: Food, size: int = 40, image_src: bytes | str | None = None) -> ft.Container:
    """A round food photo with a tinted group-icon fallback."""
    style = GROUP_AVATAR_STYLES[food.food_group]
    fallback = ft.Container(
        content=ft.Icon(style.icon, size=int(size * 0.5), color=style.ink),
        width=size,
        height=size,
        border_radius=999,
        bgcolor=style.bg,
        alignment=ft.Alignment.CENTER,
    )
    if image_src is None:
        return fallback
    return ft.Container(
        content=ft.Image(
            src=image_src,
            width=size,
            height=size,
            fit=ft.BoxFit.COVER,
            border_radius=999,
            error_content=fallback,
        ),
        width=size,
        height=size,
        border_radius=999,
        bgcolor=style.bg,
        tooltip=food.name,
    )


def food_photo(
    food: Food, width: int | None, height: int, image_src: bytes | str | None = None
) -> ft.Container:
    """A large rounded-square food photo with a tinted group-icon fallback.

    `width=None` expands to fill the available width instead of a fixed size.
    """
    style = GROUP_AVATAR_STYLES[food.food_group]
    size_kwargs = {"expand": True} if width is None else {"width": width}
    fallback_icon_size = height if width is None else min(width, height)
    fallback = ft.Container(
        content=ft.Icon(style.icon, size=int(fallback_icon_size * 0.45), color=style.ink),
        height=height,
        border_radius=theme.RADIUS_SM,
        bgcolor=style.bg,
        alignment=ft.Alignment.CENTER,
        **size_kwargs,
    )
    if image_src is None:
        return fallback
    return ft.Container(
        content=ft.Image(
            src=image_src,
            height=height,
            fit=ft.BoxFit.COVER,
            border_radius=theme.RADIUS_SM,
            error_content=fallback,
            **size_kwargs,
        ),
        height=height,
        border_radius=theme.RADIUS_SM,
        bgcolor=style.bg,
        tooltip=food.name,
        **size_kwargs,
    )


def source_mix_caption(source_mix: dict[PriceSource, int]) -> str:
    parts = [
        f"{count} {SOURCE_SHORT_NAMES[source]}"
        for source, count in sorted(source_mix.items(), key=lambda kv: kv[0].value)
    ]
    return (
        f"Prices are planning estimates from mixed sources ({', '.join(parts)}) — "
        "not a single store's checkout total."
    )


def budget_bar(
    total_cost: float,
    budget: float,
    horizon_days: int,
    caption: str,
    status_line: str | None = None,
) -> ft.Container:
    used = min(total_cost / budget, 1.0) if budget > 0 else 0.0
    children: list[ft.Control] = [
        ft.Row(
            [
                ft.Text(
                    f"${total_cost:.2f}",
                    size=28,
                    weight=ft.FontWeight.W_700,
                    color=theme.TEXT,
                ),
                muted_text(
                    f"of ${budget:.2f} · Estimated basket budget cap · "
                    f"{horizon_days}-day plan"
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.END,
        ),
        ft.ProgressBar(
            value=used,
            color=theme.PRIMARY,
            bgcolor=theme.SURFACE_TINT,
            bar_height=8,
            border_radius=999,
        ),
    ]
    if status_line:
        children.append(muted_text(status_line, size=12))
    children.append(muted_text(caption, size=12))
    return card(*children, spacing=10)


BASKET_CARD_WIDTH = theme.CARD_GRID_ITEM_WIDTH


def basket_card(
    food: Food,
    quantity_label: str,
    cost: float,
    details: list[tuple[str, str]],
    image_src: bytes | str | None = None,
    purchased: bool | None = None,
    on_purchased_change: Callable[[bool], None] | None = None,
    action_menu: ft.Control | None = None,
    pantry_note: str | None = None,
    pantry_note_tooltip: str | None = None,
) -> ft.Container:
    """One shopping-list card: big photo on top, name, quantity, and price below.

    Price-source and match details open in a dialog behind the ⓘ button.
    ``purchased`` (when not None) adds a "Purchased" button that reports the
    purchase through ``on_purchased_change(True)``; ``action_menu`` (e.g. the
    photo/receipt camera menu) renders beside it.
    """
    photo_width = BASKET_CARD_WIDTH - 24  # card padding on both sides

    def detail_line(key: str, value: str) -> ft.Row:
        return ft.Row(
            [
                ft.Container(muted_text(key, size=12), width=140),
                ft.Text(value, size=12.5, color=theme.TEXT, expand=True),
            ],
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

    def show_details(e) -> None:
        dialog = ft.AlertDialog(
            title=ft.Text(food.name, size=15, weight=ft.FontWeight.W_600, color=theme.TEXT),
            content=ft.Container(
                width=380,
                content=ft.Column(
                    [detail_line(key, value) for key, value in details],
                    spacing=6,
                    tight=True,
                ),
            ),
            actions=[ft.TextButton(content="Close", on_click=lambda ev: e.control.page.pop_dialog())],
        )
        e.control.page.show_dialog(dialog)

    info_button = ft.IconButton(
        icon=ft.Icons.INFO_OUTLINE,
        icon_size=18,
        icon_color=theme.TEXT_MUTED,
        tooltip="Price match details",
        on_click=show_details,
    )
    # Fixed-height slots for the name (1 vs 2 lines) and the optional pantry
    # note, so every card in a row is the same height regardless of content —
    # generously sized above the raw line-height math so overflow=ELLIPSIS
    # (not the container) governs any truncation.
    name_slot = ft.Container(
        content=ft.Text(
            food.name,
            size=13.5,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        ),
        height=40,
    )
    pantry_note_slot = ft.Container(
        content=(
            ft.Container(
                content=ft.Text(
                    pantry_note,
                    size=11.5,
                    weight=ft.FontWeight.W_600,
                    color=theme.PRIMARY_DARK,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                bgcolor=theme.PRIMARY_TINT,
                border_radius=999,
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                tooltip=pantry_note_tooltip,
            )
            if pantry_note
            else None
        ),
        height=26,
    )
    children: list[ft.Control] = [
        food_photo(food, photo_width, 110, image_src=image_src),
        name_slot,
        muted_text(quantity_label, size=12),
        pantry_note_slot,
        ft.Row(
            [
                ft.Text(f"${cost:.2f}", size=14, weight=ft.FontWeight.W_700, color=theme.TEXT),
                info_button,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    ]
    if purchased is not None:
        def on_purchase_click(e) -> None:
            if on_purchased_change is not None:
                on_purchased_change(True)

        purchase_row: list[ft.Control] = [
            ft.FilledTonalButton(
                content="Purchased",
                icon=ft.Icons.SHOPPING_CART_CHECKOUT,
                height=34,
                on_click=on_purchase_click,
            )
        ]
        if action_menu is not None:
            purchase_row.append(action_menu)
        children.append(ft.Row(purchase_row, spacing=4))
    foreground = ft.Column(children, spacing=6)

    if image_src is None:
        body: ft.Control = foreground
    else:
        body = ft.Stack(
            [
                ft.Image(
                    src=image_src,
                    fit=ft.BoxFit.COVER,
                    opacity=0.28,
                    border_radius=theme.RADIUS_SM,
                    error_content=ft.Container(),
                    left=0,
                    top=0,
                    right=0,
                    bottom=0,
                ),
                ft.Container(
                    left=0,
                    top=0,
                    right=0,
                    bottom=0,
                    border_radius=theme.RADIUS_SM,
                    gradient=ft.LinearGradient(
                        begin=ft.Alignment.TOP_CENTER,
                        end=ft.Alignment.BOTTOM_CENTER,
                        colors=[ft.Colors.with_opacity(0.0, theme.SURFACE), theme.SURFACE],
                        stops=[0.0, 0.55],
                    ),
                ),
                foreground,
            ]
        )

    return ft.Container(
        width=BASKET_CARD_WIDTH,
        bgcolor=theme.SURFACE,
        border=ft.Border.all(1, theme.BORDER),
        border_radius=theme.RADIUS_SM,
        padding=12,
        content=body,
    )


class GapRow(NamedTuple):
    """One nutrient's absolute amounts: planned (what the frozen meals provide)
    and covered (what current stock + purchases secure of those meals)."""

    nutrient: str
    consumed: float  # planned — the meals themselves
    purchased: float  # covered — never exceeds planned per food
    target: float

    @property
    def consumed_pct(self) -> float:
        return 100.0 * self.consumed / self.target if self.target > 0 else 100.0

    @property
    def purchased_pct(self) -> float:
        return 100.0 * self.purchased / self.target if self.target > 0 else 100.0


def nutrient_gap_row(row: GapRow, mode: str = "planned") -> ft.Container:
    """One bar + one label for the active mode ("planned" or "covered")."""
    style = NUTRIENT_STYLES[row.nutrient]
    label = Nutrients.NUTRIENT_LABELS[row.nutrient]
    if mode == "covered":
        value, pct = row.purchased, row.purchased_pct
        tooltip_line = (
            f"Your current stock and purchases cover "
            f"{nutrient_amount(value, row.nutrient)} ({pct:.0f}%) of the plan's meals."
        )
    else:
        value, pct = row.consumed, row.consumed_pct
        tooltip_line = (
            f"Your planned meals provide "
            f"{nutrient_amount(value, row.nutrient)} ({pct:.0f}%)."
        )
    bar = ft.ProgressBar(
        value=min(pct / 100.0, 1.0),
        color=style.color,
        bgcolor=ft.Colors.with_opacity(0.10, style.color),
        bar_height=8,
        border_radius=999,
    )
    return ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Row(
                            [
                                ft.Icon(style.icon, size=16, color=style.color),
                                ft.Text(label, size=13, weight=ft.FontWeight.W_500, color=theme.TEXT),
                            ],
                            spacing=6,
                        ),
                        muted_text(
                            f"{mode} {nutrient_fraction(value, row.target, row.nutrient)} · "
                            f"{pct:.0f}%",
                            size=12,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.END,
                ),
                bar,
            ],
            spacing=4,
        ),
        tooltip=(
            f"{label}: {style.blurb}\n"
            f"Target for this plan: {nutrient_amount(row.target, row.nutrient)}. "
            f"{tooltip_line}"
        ),
    )


def nutrition_card(
    rows: list[GapRow], covered_groups: set[FoodGroup], horizon_days: int
) -> ft.Container:
    """The Nutrition section: a Planned/Covered mode toggle, one bar per
    nutrient in the active mode, a "show all 12" toggle for fully-met
    nutrients, and food-group coverage chips at the bottom."""
    view = {"mode": "planned", "show_met": False}
    gap_section = ft.Column(spacing=12)

    def pct_for(row: GapRow) -> float:
        return row.purchased_pct if view["mode"] == "covered" else row.consumed_pct

    def build_gap_section() -> None:
        mode = view["mode"]
        shortfall_rows = [r for r in rows if pct_for(r) < 100.0]
        met_rows = [r for r in rows if pct_for(r) >= 100.0]

        if not shortfall_rows:
            gap_section.controls = [
                ft.Text(
                    "All 12 tracked nutrients meet target.", size=13.5, color=theme.TEXT
                ),
                *[nutrient_gap_row(row, mode) for row in rows],
            ]
            return

        met_column = ft.Column(
            [nutrient_gap_row(row, mode) for row in met_rows],
            spacing=12,
            visible=view["show_met"],
        )
        toggle_button = ft.TextButton(
            content="Hide fully-met nutrients" if view["show_met"] else "Show all 12 nutrients",
            icon=ft.Icons.EXPAND_LESS if view["show_met"] else ft.Icons.EXPAND_MORE,
        )

        def on_toggle(e) -> None:
            view["show_met"] = not view["show_met"]
            met_column.visible = view["show_met"]
            toggle_button.content = (
                "Hide fully-met nutrients" if view["show_met"] else "Show all 12 nutrients"
            )
            toggle_button.icon = (
                ft.Icons.EXPAND_LESS if view["show_met"] else ft.Icons.EXPAND_MORE
            )
            gap_section.update()

        toggle_button.on_click = on_toggle
        gap_section.controls = [
            *[nutrient_gap_row(row, mode) for row in shortfall_rows],
            *([ft.Row([toggle_button]), met_column] if met_rows else []),
        ]

    mode_toggle = ft.SegmentedButton(
        segments=[
            ft.Segment(value="planned", label=ft.Text("Planned", size=12)),
            ft.Segment(value="covered", label=ft.Text("Covered", size=12)),
        ],
        selected=["planned"],
        show_selected_icon=False,
    )

    def on_mode_change(e) -> None:
        selected = list(mode_toggle.selected or [])
        view["mode"] = selected[0] if selected else "planned"
        build_gap_section()
        gap_section.update()

    mode_toggle.on_change = on_mode_change
    mode_row = ft.Row(
        [
            ft.Container(
                content=muted_text(
                    "Planned = what the meals provide. Covered = what your "
                    "current stock and purchases secure of those meals.",
                    size=11.5,
                ),
                expand=True,
            ),
            mode_toggle,
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    build_gap_section()

    chips_column = ft.Column([group_coverage_chips(covered_groups)], visible=False)
    food_groups_toggle = ft.TextButton(
        content=f"Food groups · {len(covered_groups)} of 6", icon=ft.Icons.EXPAND_MORE
    )

    def on_food_groups_toggle(e) -> None:
        chips_column.visible = not chips_column.visible
        food_groups_toggle.icon = (
            ft.Icons.EXPAND_LESS if chips_column.visible else ft.Icons.EXPAND_MORE
        )
        food_groups_block.update()

    food_groups_toggle.on_click = on_food_groups_toggle
    food_groups_block = ft.Column([ft.Row([food_groups_toggle]), chips_column], spacing=8)

    return collapsible_section(
        "Nutrition",
        mode_row,
        gap_section,
        ft.Divider(height=1, color=theme.BORDER),
        food_groups_block,
        icon=ft.Icons.MONITOR_HEART,
        icon_tooltip="How close the plan comes to the household's nutrient targets",
        source_note=(
            "Nutrient values are simplified from USDA FoodData Central. Food "
            "groups follow a 6-category USDA MyPlate-style split."
        ),
        subtitle=f"What your household eats across the {horizon_days}-day plan",
        expanded=True,
    )


def group_coverage_chips(covered_groups: set[FoodGroup]) -> ft.Row:
    chips: list[ft.Control] = []
    for group in FoodGroup:
        covered = group in covered_groups
        label = ft.Text(
            FOOD_GROUP_LABELS[group],
            size=12,
            weight=ft.FontWeight.W_500,
            color=theme.PRIMARY_DARK if covered else theme.TEXT_MUTED,
        )
        inner: list[ft.Control] = [label]
        if covered:
            inner.insert(0, ft.Icon(ft.Icons.CHECK, size=13, color=theme.PRIMARY_DARK))
        chips.append(
            ft.Container(
                content=ft.Row(inner, spacing=4),
                bgcolor=theme.PRIMARY_TINT if covered else theme.SURFACE,
                border=None if covered else ft.Border.all(1, theme.BORDER),
                border_radius=999,
                padding=ft.Padding.symmetric(horizontal=12, vertical=5),
            )
        )
    return ft.Row(chips, wrap=True, spacing=8, run_spacing=8)


def warnings_strip(
    nutrition_feasible: bool,
    budget_status: BudgetStatus,
    relaxed_constraints: tuple[str, ...],
    dominance_flags: tuple[str, ...],
) -> ft.Container | None:
    if nutrition_feasible and budget_status is BudgetStatus.WITHIN:
        return None
    if budget_status is BudgetStatus.OVER:
        # The accurate dollar detail is already in relaxed_constraints below.
        headline = "Over the estimated basket budget cap."
    elif budget_status is BudgetStatus.UNKNOWN:
        headline = "Estimated-cap status unknown — price data is incomplete."
    else:
        headline = (
            "Within the estimated basket budget cap — nutrition targets partially met."
        )
    lines: list[ft.Control] = [
        ft.Text(headline, size=13.5, weight=ft.FontWeight.W_600, color=theme.WARN_INK)
    ]
    for constraint in relaxed_constraints:
        lines.append(ft.Text(f"•  {constraint}", size=12.5, color=theme.WARN_INK))
    for flag in dominance_flags:
        lines.append(ft.Text(f"•  Flagged: {flag}", size=12.5, color=theme.WARN_INK))
    return card(
        ft.Row(
            [
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=18, color=theme.WARN_INK),
                ft.Column(lines, spacing=4, expand=True),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
        ),
        bgcolor=theme.WARN_BG,
        border_color=theme.WARN_BORDER,
        padding=16,
    )
