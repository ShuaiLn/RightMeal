"""Calendar page: the saved plan on real dates — a read-only overview.

Tracking (Eaten, leftovers, undo) lives on the Plan tab's meal cards; this
page just reflects that state and deep-links back to the right day.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Callable

import flet as ft

import theme
from models import SLOT_LABELS, SLOT_ORDER, Nutrients
from services.nutrition import NutrientStatus, eaten_day_status, suggest_foods_for
from ui.components import (
    NUTRIENT_STYLES,
    card,
    food_avatar,
    muted_text,
    pill,
    primary_button,
    section_card,
)
from ui.meals_section import SLOT_ICONS, dish_or_ingredient_photo, portion_label
from ui.state import AppState

_WEEKDAY_HEADERS = ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")


def _fmt_date_long(when: date) -> str:
    return f"{when.strftime('%A')}, {when.strftime('%b')} {when.day}"


_LEVEL_COLORS: dict[str, tuple[str, str]] = {
    "sufficient": (theme.PRIMARY_TINT, theme.PRIMARY_DARK),
    "borderline": (theme.WARN_BG, theme.WARN_INK),
    "lacking": (theme.DANGER_TINT, theme.DANGER),
}


def _status_label(status: NutrientStatus, is_final_day: bool) -> str:
    """Non-clinical, mid-day-aware copy: a "lacking" verdict is premature and
    alarming for a day still in progress, so it reads as "so far today"
    instead of a final-sounding "Lacking X" until the day is actually over."""
    name = Nutrients.NUTRIENT_LABELS[status.nutrient]
    if status.level == "sufficient":
        return f"On track for {name}"
    if status.level == "lacking":
        return f"Lacking {name}" if is_final_day else f"{name}: low so far today"
    return f"{name}: below target" if is_final_day else f"{name}: getting there"


def _status_chip(status: NutrientStatus, is_final_day: bool) -> ft.Control:
    style = NUTRIENT_STYLES[status.nutrient]
    bg, ink = _LEVEL_COLORS[status.level]
    return ft.Row(
        [
            ft.Icon(style.icon, size=14, color=style.color),
            pill(_status_label(status, is_final_day), bg, ink),
        ],
        spacing=4,
    )


def _eaten_nutrition_summary(state: AppState, plan, when: date, day_plan) -> ft.Control:
    """A green/red pill per nutrient for what was actually eaten this day,
    capped to Calories + the two lowest-coverage nutrients by default, plus a
    tip naming diet-safe foods for the single worst shortfall."""
    header = ft.Text("Today's nutrition", size=13.5, weight=ft.FontWeight.W_600, color=theme.TEXT)
    eaten_nutrients = Nutrients()
    any_eaten = False
    for meal in day_plan.meals:
        if plan.tracking_entry(when, meal.slot)["eaten"]:
            any_eaten = True
            eaten_nutrients = eaten_nutrients.plus(meal.nutrients)

    if not any_eaten or state.profile is None:
        return ft.Column(
            [header, muted_text("No meals logged yet today.", size=12.5)], spacing=8
        )

    targets = state.nutrition.household_daily_targets(state.profile)
    statuses = eaten_day_status(eaten_nutrients, targets)
    is_final_day = when < date.today()
    by_nutrient = {s.nutrient: s for s in statuses}

    ranked = sorted(
        (s for s in statuses if s.nutrient != "calories_kcal" and s.pct is not None),
        key=lambda s: s.pct,
    )
    default_visible = [by_nutrient["calories_kcal"], *ranked[:2]]
    rest = [s for s in statuses if s not in default_visible]

    visible_row = ft.Row(
        [_status_chip(s, is_final_day) for s in default_visible],
        wrap=True, spacing=8, run_spacing=8,
    )
    rest_row = ft.Row(
        [_status_chip(s, is_final_day) for s in rest],
        wrap=True, spacing=8, run_spacing=8, visible=False,
    )
    toggle_button = ft.TextButton(content="Show all nutrients", icon=ft.Icons.EXPAND_MORE)
    summary = ft.Column([header, visible_row, rest_row, ft.Row([toggle_button])], spacing=8)

    def on_toggle(e) -> None:
        rest_row.visible = not rest_row.visible
        toggle_button.content = (
            "Hide extra nutrients" if rest_row.visible else "Show all nutrients"
        )
        toggle_button.icon = (
            ft.Icons.EXPAND_LESS if rest_row.visible else ft.Icons.EXPAND_MORE
        )
        summary.update()

    toggle_button.on_click = on_toggle

    lacking = [s for s in statuses if s.level == "lacking"]
    borderline = [s for s in statuses if s.level == "borderline"]
    worst = min(lacking, key=lambda s: s.pct) if lacking else (
        min(borderline, key=lambda s: s.pct) if borderline else None
    )
    if worst is not None:
        suggestions = suggest_foods_for(worst.nutrient, state.foods, state.profile)
        if suggestions:
            names = [food.name for food in suggestions]
            joined = names[0] if len(names) == 1 else (
                f"{', '.join(names[:-1])}, or {names[-1]}"
            )
            label = Nutrients.NUTRIENT_LABELS[worst.nutrient]
            # Lowercase only the leading word (e.g. "Vitamin D" -> "vitamin D",
            # not "vitamin d") so single-letter suffixes stay readable.
            first_word, _, rest_of_label = label.partition(" ")
            lowered_label = f"{first_word.lower()}{' ' + rest_of_label if rest_of_label else ''}"
            summary.controls.append(
                muted_text(f"Tip: add more {lowered_label} — try {joined}.", size=12)
            )

    return summary


def build_calendar_view(
    page: ft.Page,
    state: AppState,
    on_go_to_plan: Callable[[], None],
    on_open_in_plan: Callable[[date], None],
) -> ft.Control:
    plan = state.saved_plan
    if plan is None:
        return card(
            ft.Column(
                [
                    ft.Icon(ft.Icons.CALENDAR_MONTH, size=48, color=theme.TEXT_MUTED),
                    ft.Text(
                        "No plan on the calendar yet",
                        size=16,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT,
                    ),
                    muted_text("Build a grocery plan and it shows up here, day by day."),
                    ft.Container(height=4),
                    _go_to_plan_button(on_go_to_plan),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=40,
        )

    today = date.today()
    selected_date = today if plan.start_date <= today <= plan.end_date else plan.start_date
    shown = [selected_date.year, selected_date.month]

    month_label = ft.Text("", size=15, weight=ft.FontWeight.W_600, color=theme.TEXT)
    grid_column = ft.Column(spacing=4)
    detail_column = ft.Column(spacing=12)

    def cached_src(food) -> bytes | None:
        # Calendar renders from disk only — no network fallback.
        return state.image_cache.get_cached(food.image_url)

    def tracking_dots(when: date) -> list[ft.Control]:
        day_plan = plan.day_for_date(when)
        if day_plan is None or not day_plan.meals:
            return []
        dots: list[ft.Control] = []
        for meal in day_plan.meals[:3]:
            eaten = plan.tracking_entry(when, meal.slot)["eaten"]
            dots.append(
                ft.Container(
                    width=6,
                    height=6,
                    border_radius=999,
                    bgcolor=theme.PRIMARY if eaten else theme.BORDER,
                )
            )
        return dots

    def select_day(when: date):
        def handler(e) -> None:
            nonlocal selected_date
            selected_date = when
            rebuild_grid()
            rebuild_detail()
            page.update()

        return handler

    def day_cell(day_number: int) -> ft.Container:
        if day_number == 0:
            return ft.Container(expand=1, height=64)
        when = date(shown[0], shown[1], day_number)
        in_plan = plan.start_date <= when <= plan.end_date
        is_selected = when == selected_date
        return ft.Container(
            expand=1,
            height=64,
            ink=True,
            on_click=select_day(when),
            border_radius=theme.RADIUS_SM,
            bgcolor=theme.PRIMARY_TINT if is_selected else (theme.SURFACE_TINT if in_plan else None),
            border=ft.Border.all(1, theme.PRIMARY) if when == today else None,
            padding=ft.Padding.only(top=8),
            content=ft.Column(
                [
                    ft.Text(
                        str(day_number),
                        size=12.5,
                        weight=ft.FontWeight.W_600 if in_plan else ft.FontWeight.W_400,
                        color=theme.PRIMARY_DARK if is_selected else theme.TEXT,
                    ),
                    ft.Row(tracking_dots(when), spacing=3, alignment=ft.MainAxisAlignment.CENTER),
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def rebuild_grid() -> None:
        year, month = shown
        month_label.value = f"{calendar.month_name[month]} {year}"
        rows: list[ft.Control] = [
            ft.Row(
                [
                    ft.Container(
                        expand=1,
                        content=muted_text(header, size=11.5),
                        alignment=ft.Alignment.CENTER,
                    )
                    for header in _WEEKDAY_HEADERS
                ]
            )
        ]
        month_weeks = calendar.Calendar(firstweekday=calendar.SUNDAY).monthdayscalendar(year, month)
        for week in month_weeks:
            rows.append(ft.Row([day_cell(day) for day in week], spacing=4))
        grid_column.controls = rows

    def shift_month(delta: int):
        def handler(e) -> None:
            year, month = shown
            month += delta
            if month < 1:
                year, month = year - 1, 12
            elif month > 12:
                year, month = year + 1, 1
            shown[0], shown[1] = year, month
            rebuild_grid()
            page.update()

        return handler

    def status_block(when: date, meal) -> ft.Control:
        """Read-only tracking state; all actions live on the Plan tab."""
        entry = plan.tracking_entry(when, meal.slot)
        rows: list[ft.Control] = []
        if entry["eaten"]:
            chips: list[ft.Control] = [pill("Eaten ✓", theme.PRIMARY_TINT, theme.PRIMARY_DARK)]
            created_id = entry.get("leftover_created_id")
            record = state.leftovers_by_id.get(created_id) if created_id else None
            if record is not None:
                saved_pct = round(record.initial_fraction_remaining * 100)
                chips.append(pill(
                    f"{100 - saved_pct}% eaten · {saved_pct}% saved",
                    theme.SURFACE_TINT, theme.TEXT_MUTED,
                ))
            rows.append(ft.Row(chips, spacing=6))
            note = entry.get("leftover_note", "")
            if note:
                rows.append(ft.Text(
                    f"“{note}”", size=12, color=theme.TEXT_MUTED,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, tooltip=note,
                ))
        else:
            rows.append(muted_text("Track this meal from the Plan tab.", size=12))
        if meal.prepared_leftover_id is not None:
            rows.append(muted_text(
                "Prepared leftover — eating it uses no pantry ingredients.", size=12
            ))
        elif meal.is_leftover:
            rows.append(muted_text("Ingredients were used with the batch dinner.", size=12))
        return ft.Column(rows, spacing=6)

    def meal_card(when: date, meal) -> ft.Container:
        name_row: list[ft.Control] = [
            ft.Text(meal.name, size=13.5, weight=ft.FontWeight.W_600, color=theme.TEXT)
        ]
        if meal.prepared_leftover_id is not None:
            name_row.append(pill("prepared leftovers", theme.SURFACE_TINT, theme.TEXT_MUTED))
        elif meal.is_leftover:
            name_row.append(pill("leftovers", theme.SURFACE_TINT, theme.TEXT_MUTED))
        dish = dish_or_ingredient_photo(meal, state, size=44, width=44)
        pp = meal.per_person_kcal
        kcal_text = (f"≈{pp:,.0f} kcal/person" if pp is not None and meal.servings > 0
                     else f"≈{meal.kcal:,.0f} kcal")
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(SLOT_ICONS[meal.slot], size=15, color=theme.PRIMARY_DARK),
                            ft.Text(
                                SLOT_LABELS[meal.slot],
                                size=12,
                                weight=ft.FontWeight.W_600,
                                color=theme.PRIMARY_DARK,
                            ),
                            ft.Container(expand=True),
                            muted_text(kcal_text, size=12),
                        ],
                        spacing=6,
                    ),
                    ft.Row([dish, ft.Row(name_row, spacing=8, expand=True)], spacing=10),
                    muted_text(" · ".join(portion_label(p) for p in meal.portions), size=12),
                    status_block(when, meal),
                ],
                spacing=8,
            ),
            bgcolor=theme.SURFACE_TINT,
            border_radius=theme.RADIUS_SM,
            padding=12,
        )

    def rebuild_detail() -> None:
        day_plan = plan.day_for_date(selected_date)
        header: list[ft.Control] = [
            ft.Text(_fmt_date_long(selected_date), size=15, weight=ft.FontWeight.W_600,
                    color=theme.TEXT, expand=True),
        ]
        if day_plan is not None:
            open_day = selected_date
            header.append(ft.TextButton(
                content="Open in Plan",
                icon=ft.Icons.SHOPPING_BASKET_OUTLINED,
                tooltip="Jump to this day's meal cards to track them",
                on_click=lambda e: on_open_in_plan(open_day),
            ))
        controls: list[ft.Control] = [
            ft.Row(header, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        ]
        if day_plan is None:
            controls.append(muted_text("No meals planned this day — it's outside the current plan."))
        elif not day_plan.meals:
            controls.append(muted_text("No meals could be scheduled for this day."))
        else:
            for slot in SLOT_ORDER:
                meal = day_plan.meal_for(slot)
                if meal is not None:
                    controls.append(meal_card(selected_date, meal))
                else:
                    controls.append(
                        muted_text(f"{SLOT_LABELS[slot]}: no meal planned.", size=12.5)
                    )
            controls.append(ft.Divider(height=1, color=theme.BORDER))
            controls.append(_eaten_nutrition_summary(state, plan, selected_date, day_plan))
        detail_column.controls = controls

    rebuild_grid()
    rebuild_detail()

    grid_card = card(
        ft.Row(
            [
                ft.IconButton(
                    icon=ft.Icons.CHEVRON_LEFT,
                    icon_color=theme.TEXT_MUTED,
                    tooltip="Previous month",
                    on_click=shift_month(-1),
                ),
                ft.Container(content=month_label, expand=True, alignment=ft.Alignment.CENTER),
                ft.IconButton(
                    icon=ft.Icons.CHEVRON_RIGHT,
                    icon_color=theme.TEXT_MUTED,
                    tooltip="Next month",
                    on_click=shift_month(1),
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        grid_column,
        muted_text(
            f"Plan: {plan.start_date.strftime('%b')} {plan.start_date.day} – "
            f"{plan.end_date.strftime('%b')} {plan.end_date.day} · dots turn green as meals "
            "are marked eaten",
            size=12,
        ),
        spacing=10,
    )

    return ft.Column(
        [grid_card, section_card(None, detail_column)],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )


def _go_to_plan_button(on_go_to_plan: Callable[[], None]) -> ft.FilledButton:
    button = primary_button("Go to Start", icon=ft.Icons.ROCKET_LAUNCH_OUTLINED)
    button.on_click = lambda e: on_go_to_plan()
    return button
