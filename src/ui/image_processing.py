"""Reusable modal progress and failure-detail views for image workflows."""

from __future__ import annotations

from dataclasses import dataclass

import flet as ft

import theme


@dataclass(frozen=True)
class ImageFailureDetails:
    """Privacy-safe information shown after an image workflow fails."""

    summary: str
    stage: str
    reason: str
    suggestions: tuple[str, ...] = ()
    diagnostics: tuple[tuple[str, str], ...] = ()


class ImageProcessingView:
    """One modal that transitions from loading to a failure-detail page."""

    def __init__(self, page: ft.Page, *, title: str = "Processing image") -> None:
        self.page = page
        self.title = title
        self._active = False
        self.dialog = ft.AlertDialog(modal=True)

    @property
    def active(self) -> bool:
        return self._active

    def show(self, message: str, detail: str = "This may take a moment.") -> None:
        self.dialog.title = ft.Text(
            self.title,
            size=16,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
        )
        self.dialog.content = ft.Container(
            width=430,
            padding=ft.Padding.symmetric(vertical=18, horizontal=8),
            content=ft.Column(
                [
                    ft.ProgressRing(
                        width=44,
                        height=44,
                        stroke_width=4,
                        color=theme.PRIMARY,
                    ),
                    ft.Text(
                        message,
                        size=14,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        detail,
                        size=12,
                        color=theme.TEXT_MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
                spacing=14,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
        )
        self.dialog.actions = []
        self._present()

    def show_failure(self, details: ImageFailureDetails) -> None:
        rows: list[ft.Control] = [
            ft.Row(
                [
                    ft.Icon(ft.Icons.ERROR_OUTLINE, color=theme.DANGER, size=26),
                    ft.Text(
                        "Image processing failed",
                        size=16,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT,
                    ),
                ],
                spacing=10,
            ),
            ft.Text(details.summary, size=13, color=theme.TEXT),
            ft.Divider(height=1, color=theme.BORDER),
            _detail_row("Failed stage", details.stage),
            _detail_row("Reason", details.reason),
        ]
        if details.diagnostics:
            rows.extend(
                [
                    ft.Text(
                        "Processing details",
                        size=12.5,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT,
                    ),
                    *(
                        _detail_row(label, value)
                        for label, value in details.diagnostics
                        if value
                    ),
                ]
            )
        if details.suggestions:
            rows.extend(
                [
                    ft.Text(
                        "What you can try",
                        size=12.5,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT,
                    ),
                    *(
                        ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.ARROW_RIGHT,
                                    size=15,
                                    color=theme.TEXT_MUTED,
                                ),
                                ft.Text(
                                    suggestion,
                                    size=12,
                                    color=theme.TEXT_MUTED,
                                    expand=True,
                                ),
                            ],
                            spacing=6,
                        )
                        for suggestion in details.suggestions
                    ),
                ]
            )
        self.dialog.title = None
        self.dialog.content = ft.Container(
            width=500,
            height=430,
            content=ft.Column(rows, spacing=11, scroll=ft.ScrollMode.AUTO),
        )
        self.dialog.actions = [
            ft.TextButton(content="Close", on_click=lambda event: self.close())
        ]
        self._present()

    def close(self) -> None:
        if not self._active:
            return
        self.page.pop_dialog()
        self._active = False

    def _present(self) -> None:
        if self._active:
            self.page.update()
        else:
            self.page.show_dialog(self.dialog)
            self._active = True


def _detail_row(label: str, value: str) -> ft.Row:
    return ft.Row(
        [
            ft.Text(
                label,
                width=118,
                size=11.5,
                weight=ft.FontWeight.W_600,
                color=theme.TEXT_MUTED,
            ),
            ft.Text(value, size=11.5, color=theme.TEXT, expand=True, selectable=True),
        ],
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )
