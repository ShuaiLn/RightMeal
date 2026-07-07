"""Shared UI building blocks."""

from __future__ import annotations

import flet as ft

import theme
from models import (
    FOOD_GROUP_LABELS,
    PRICE_SOURCE_LABELS,
    FoodGroup,
    Nutrients,
    OptimizationResult,
    PriceQuote,
    PriceSource,
)

SOURCE_SHORT_NAMES = {
    PriceSource.KROGER_REAL_PRICE: "Kroger",
    PriceSource.INSTACART_NUMERIC_PRICE: "Instacart",
    PriceSource.BLS_REGIONAL_AVERAGE: "BLS",
    PriceSource.SEED_ESTIMATE: "seed",
}


def section_card(title: str | None, *controls: ft.Control) -> ft.Container:
    """A light-green card with an optional heading."""
    children: list[ft.Control] = []
    if title:
        children.append(ft.Text(title, size=16, weight=ft.FontWeight.BOLD, color=theme.TEXT_DARK))
    children.extend(controls)
    return ft.Container(
        content=ft.Column(children, spacing=10),
        bgcolor=theme.SURFACE_GREEN,
        border_radius=12,
        padding=16,
    )


def highlight_card(*controls: ft.Control, bgcolor: str = theme.HILITE_YELLOW) -> ft.Container:
    """A light-yellow card for budget/status content."""
    return ft.Container(
        content=ft.Column(list(controls), spacing=8),
        bgcolor=bgcolor,
        border_radius=12,
        padding=16,
    )


def muted_text(value: str, size: int = 12) -> ft.Text:
    return ft.Text(value, size=size, color=theme.TEXT_MUTED)


def source_chip(quote: PriceQuote) -> ft.Container:
    label = PRICE_SOURCE_LABELS[quote.source]
    if quote.is_estimate:
        label += " (est.)"
    return ft.Container(
        content=ft.Text(label, size=11, color=theme.TEXT_DARK),
        bgcolor=theme.SOURCE_CHIP_COLORS[quote.source.value],
        border_radius=999,
        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
        tooltip=quote.match_reason,
    )


def source_mix_caption(result: OptimizationResult) -> str:
    parts = [
        f"{count} {SOURCE_SHORT_NAMES[source]}"
        for source, count in sorted(result.source_mix.items(), key=lambda kv: kv[0].value)
    ]
    return (
        f"Prices are planning estimates from mixed sources ({', '.join(parts)}) — "
        "not a single store's checkout total."
    )


def budget_bar(result: OptimizationResult) -> ft.Container:
    used = min(result.total_cost / result.budget, 1.0) if result.budget > 0 else 0.0
    return highlight_card(
        ft.Text(
            f"Estimated planning total: ${result.total_cost:.2f} of ${result.budget:.2f} "
            f"({result.horizon_days} days)",
            size=16,
            weight=ft.FontWeight.BOLD,
            color=theme.TEXT_DARK,
        ),
        ft.ProgressBar(value=used, color=theme.DEEP_GREEN, bgcolor=theme.BG_WHITE, bar_height=8),
        muted_text(source_mix_caption(result)),
    )


def group_coverage_row(result: OptimizationResult) -> ft.Column:
    chips: list[ft.Control] = []
    for group in FoodGroup:
        covered = group in result.group_coverage
        chips.append(
            ft.Container(
                content=ft.Text(
                    FOOD_GROUP_LABELS[group],
                    size=11,
                    color=theme.TEXT_DARK if covered else theme.TEXT_MUTED,
                ),
                bgcolor=theme.ACCENT_GREEN if covered else theme.BG_WHITE,
                border=None if covered else ft.Border.all(1, theme.BORDER_GREEN),
                border_radius=999,
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            )
        )
    return ft.Column(
        [
            ft.Text(
                f"Food groups: {result.groups_covered} of 6 covered",
                size=14,
                weight=ft.FontWeight.BOLD,
                color=theme.TEXT_DARK,
            ),
            ft.Row(chips, wrap=True, spacing=6, run_spacing=6),
        ],
        spacing=8,
    )


def gaps_card(result: OptimizationResult) -> ft.Container:
    if not result.gaps:
        body: list[ft.Control] = [
            ft.Text(
                "No gaps — all 12 tracked nutrients meet the planning targets.",
                color=theme.TEXT_DARK,
            )
        ]
    else:
        body = []
        for gap in result.gaps:
            label = Nutrients.NUTRIENT_LABELS[gap.nutrient]
            body.append(
                ft.Column(
                    [
                        ft.Text(
                            f"{label} — {gap.pct:.0f}% of the {result.horizon_days}-day target",
                            size=13,
                            color=theme.TEXT_DARK,
                        ),
                        ft.ProgressBar(
                            value=min(gap.pct / 100.0, 1.0),
                            color=theme.DEEP_GREEN,
                            bgcolor=theme.BG_WHITE,
                            bar_height=5,
                        ),
                    ],
                    spacing=3,
                )
            )
    return section_card("Nutrition gaps", *body)


def warnings_strip(result: OptimizationResult) -> ft.Container | None:
    if result.nutrition_feasible and result.budget_feasible:
        return None
    if not result.budget_feasible:
        headline = "The budget is too low to buy any food package."
    else:
        headline = "Within budget — nutrition targets partially met."
    lines: list[ft.Control] = [
        ft.Text(headline, weight=ft.FontWeight.BOLD, color=theme.TEXT_DARK)
    ]
    for constraint in result.relaxed_constraints:
        lines.append(ft.Text(f"• {constraint}", size=12, color=theme.TEXT_DARK))
    for flag in result.dominance_flags:
        lines.append(ft.Text(f"• Flagged: {flag}", size=12, color=theme.TEXT_DARK))
    return highlight_card(*lines, bgcolor=theme.WARN_AMBER)
