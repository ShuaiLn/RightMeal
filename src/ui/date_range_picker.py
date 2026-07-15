"""A start/end date-range picker dialog built on a month grid."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Callable

import flet as ft

import theme
from ui.components import muted_text

_WEEKDAY_HEADERS = ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")

MAX_RANGE_DAYS = 14


def _fmt(when: date) -> str:
    return f"{when.strftime('%b')} {when.day}"


def range_label(start: date, end: date) -> str:
    days = (end - start).days + 1
    return f"{_fmt(start)} – {_fmt(end)} · {days} day{'s' if days > 1 else ''}"


def show_range_picker(
    page: ft.Page,
    start: date,
    end: date,
    on_picked: Callable[[date, date], None],
    max_days: int = MAX_RANGE_DAYS,
) -> None:
    """Open a dialog where the first click picks the start day, the second the end day."""
    today = date.today()
    first_selectable = today - timedelta(days=1)
    sel: dict[str, date | None] = {"start": start, "end": end}
    shown = [start.year, start.month]

    month_label = ft.Text("", size=14.5, weight=ft.FontWeight.W_600, color=theme.TEXT)
    grid_column = ft.Column(spacing=4)
    hint = muted_text("", size=12)

    def selection_hint() -> str:
        picked_start, picked_end = sel["start"], sel["end"]
        if picked_start is not None and picked_end is not None:
            return range_label(picked_start, picked_end)
        if picked_start is not None:
            return f"{_fmt(picked_start)} – now pick the last day"
        return "Pick the first day"

    def on_day_click(when: date):
        def handler(e) -> None:
            error = ""
            picked_start, picked_end = sel["start"], sel["end"]
            if picked_start is None or picked_end is not None:
                sel["start"], sel["end"] = when, None
            elif when < picked_start:
                sel["start"] = when
            elif (when - picked_start).days + 1 > max_days:
                error = f"Plans can cover at most {max_days} days."
            else:
                sel["end"] = when
            rebuild_grid()
            hint.value = error or selection_hint()
            page.update()

        return handler

    def day_cell(day_number: int) -> ft.Container:
        if day_number == 0:
            return ft.Container(width=40, height=40)
        when = date(shown[0], shown[1], day_number)
        disabled = when < first_selectable
        is_edge = when in (sel["start"], sel["end"])
        picked_start, picked_end = sel["start"], sel["end"]
        in_range = (
            picked_start is not None
            and picked_end is not None
            and picked_start < when < picked_end
        )
        if disabled:
            text_color = ft.Colors.with_opacity(0.4, theme.TEXT_MUTED)
        elif is_edge:
            text_color = theme.SURFACE
        else:
            text_color = theme.PRIMARY_DARK if in_range else theme.TEXT
        return ft.Container(
            width=40,
            height=40,
            border_radius=999,
            ink=not disabled,
            on_click=None if disabled else on_day_click(when),
            bgcolor=theme.PRIMARY if is_edge else (theme.PRIMARY_TINT if in_range else None),
            border=ft.Border.all(1, theme.PRIMARY) if when == today and not is_edge else None,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(
                str(day_number),
                size=12.5,
                weight=ft.FontWeight.W_600 if is_edge else ft.FontWeight.W_400,
                color=text_color,
            ),
        )

    def rebuild_grid() -> None:
        year, month = shown
        month_label.value = f"{calendar.month_name[month]} {year}"
        rows: list[ft.Control] = [
            ft.Row(
                [
                    ft.Container(
                        width=40,
                        content=muted_text(header, size=11.5),
                        alignment=ft.Alignment.CENTER,
                    )
                    for header in _WEEKDAY_HEADERS
                ],
                spacing=4,
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

    def on_cancel(e) -> None:
        page.pop_dialog()

    def on_done(e) -> None:
        picked_start = sel["start"]
        if picked_start is None:
            return
        picked_end = sel["end"] or picked_start
        page.pop_dialog()
        on_picked(picked_start, picked_end)

    rebuild_grid()
    hint.value = selection_hint()

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Plan dates", size=15, weight=ft.FontWeight.W_600, color=theme.TEXT),
        content=ft.Container(
            width=320,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.IconButton(
                                icon=ft.Icons.CHEVRON_LEFT,
                                icon_color=theme.TEXT_MUTED,
                                tooltip="Previous month",
                                on_click=shift_month(-1),
                            ),
                            ft.Container(
                                content=month_label, expand=True, alignment=ft.Alignment.CENTER
                            ),
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
                    hint,
                ],
                spacing=8,
                tight=True,
            ),
        ),
        actions=[
            ft.TextButton(content="Cancel", on_click=on_cancel),
            ft.TextButton(content="Done", on_click=on_done),
        ],
    )
    page.show_dialog(dialog)
