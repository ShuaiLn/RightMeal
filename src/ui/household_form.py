"""Household profile form shared by the onboarding and profile views."""

from __future__ import annotations

import re

import flet as ft

import theme
from models import HouseholdProfile
from ui.components import muted_text, section_card, style_field

ALLERGEN_SUGGESTIONS = ("peanut", "tree nut", "egg", "dairy", "soy", "fish", "shellfish", "gluten")
ZIP_RE = re.compile(r"^\d{5}$")

PRIVACY_NOTE = (
    "RightMeal stores profile data locally only — no accounts, no cloud sync. "
    "Delete profile.json (or use the Delete saved data button) to remove it."
)


class HouseholdForm:
    """Builds the profile form controls and reads a HouseholdProfile back out."""

    def __init__(self, page: ft.Page, profile: HouseholdProfile | None):
        self.page = page
        base = profile or HouseholdProfile(adults=2, children=0, seniors=0)

        self.counts = {"adults": base.adults, "children": base.children, "seniors": base.seniors}
        self.count_texts = {
            key: ft.Text(str(value), size=14, weight=ft.FontWeight.W_600, color=theme.TEXT)
            for key, value in self.counts.items()
        }
        self.members_error = ft.Text("", size=12, color=theme.DANGER, visible=False)

        self.vegetarian = ft.Checkbox(label="Vegetarian", value=base.vegetarian)
        self.no_pork = ft.Checkbox(label="No pork", value=base.no_pork)
        self.lactose_free = ft.Checkbox(label="Lactose-free", value=base.lactose_free)

        self.allergies: list[str] = list(base.allergies)
        self.allergy_input = ft.TextField(label="Add an allergy", width=220)
        self.allergy_chips = ft.Row(wrap=True, spacing=8, run_spacing=8)

        self.city = ft.TextField(label="City", value=base.city, width=260)
        self.zip_code = ft.TextField(label="ZIP code", value=base.zip_code, width=140)
        for field in (self.allergy_input, self.city, self.zip_code):
            style_field(field)

        self._rebuild_allergy_chips()

    # --- counts -----------------------------------------------------------

    def _stepper(self, key: str, label: str) -> ft.Row:
        def change(delta: int):
            def handler(e):
                self.counts[key] = max(0, self.counts[key] + delta)
                self.count_texts[key].value = str(self.counts[key])
                self.page.update()

            return handler

        stepper = ft.Container(
            content=ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.REMOVE,
                        on_click=change(-1),
                        icon_size=16,
                        icon_color=theme.TEXT_MUTED,
                    ),
                    ft.Container(
                        content=self.count_texts[key], width=30, alignment=ft.Alignment.CENTER
                    ),
                    ft.IconButton(
                        icon=ft.Icons.ADD,
                        on_click=change(1),
                        icon_size=16,
                        icon_color=theme.TEXT_MUTED,
                    ),
                ],
                spacing=0,
            ),
            border=ft.Border.all(1, theme.BORDER),
            border_radius=999,
        )
        return ft.Row(
            [ft.Text(label, size=13.5, color=theme.TEXT, expand=True), stepper],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    # --- allergies ----------------------------------------------------------

    def _rebuild_allergy_chips(self) -> None:
        def delete_handler(allergen: str):
            def handler(e):
                self.allergies.remove(allergen)
                self._rebuild_allergy_chips()
                self.page.update()

            return handler

        self.allergy_chips.controls = [
            ft.Chip(
                label=allergen,
                on_delete=delete_handler(allergen),
                bgcolor=theme.WARN_BG,
                shape=ft.StadiumBorder(),
                border_side=ft.BorderSide(1, theme.WARN_BORDER),
                delete_icon_color=theme.WARN_INK,
                label_text_style=ft.TextStyle(size=12.5, color=theme.WARN_INK),
            )
            for allergen in self.allergies
        ]

    def _add_allergy(self, raw: str) -> None:
        allergen = raw.strip().lower()
        if allergen and allergen not in self.allergies:
            self.allergies.append(allergen)
            self._rebuild_allergy_chips()
        self.allergy_input.value = ""
        self.page.update()

    def _suggestion_chips(self) -> ft.Row:
        def add_handler(allergen: str):
            def handler(e):
                self._add_allergy(allergen)

            return handler

        return ft.Row(
            [
                ft.Container(
                    content=ft.Text(f"+ {allergen}", size=12, color=theme.TEXT_MUTED),
                    border=ft.Border.all(1, theme.BORDER),
                    border_radius=999,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                    ink=True,
                    on_click=add_handler(allergen),
                )
                for allergen in ALLERGEN_SUGGESTIONS
            ],
            wrap=True,
            spacing=8,
            run_spacing=8,
        )

    # --- assembly -----------------------------------------------------------

    def build(self) -> ft.Column:
        return ft.Column(
            [
                section_card(
                    "Household members",
                    self._stepper("adults", "Adults"),
                    self._stepper("children", "Children"),
                    self._stepper("seniors", "Seniors"),
                    self.members_error,
                ),
                section_card(
                    "Dietary restrictions",
                    ft.Row([self.vegetarian, self.no_pork, self.lactose_free], wrap=True),
                    ft.Text(
                        "Allergies (always hard exclusions)",
                        size=13,
                        weight=ft.FontWeight.W_500,
                        color=theme.TEXT,
                    ),
                    ft.Row(
                        [
                            self.allergy_input,
                            ft.Button(
                                content="Add",
                                on_click=lambda e: self._add_allergy(self.allergy_input.value),
                                style=ft.ButtonStyle(
                                    shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM)
                                ),
                            ),
                        ],
                        spacing=8,
                    ),
                    self.allergy_chips,
                    self._suggestion_chips(),
                ),
                section_card(
                    "Default location (U.S.)",
                    ft.Row([self.city, self.zip_code], wrap=True, spacing=10),
                ),
                ft.Row(
                    [
                        ft.Icon(ft.Icons.LOCK_OUTLINE, size=14, color=theme.TEXT_MUTED),
                        muted_text(PRIVACY_NOTE, size=12),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            ],
            spacing=16,
        )

    def validate(self) -> bool:
        ok = True
        total = sum(self.counts.values())
        self.members_error.visible = total < 1
        self.members_error.value = "The household needs at least one member."
        if total < 1:
            ok = False
        if not ZIP_RE.match(self.zip_code.value or ""):
            self.zip_code.error = "Enter a 5-digit U.S. ZIP code"
            ok = False
        else:
            self.zip_code.error = None
        self.page.update()
        return ok

    def read_profile(self, existing: HouseholdProfile | None = None) -> HouseholdProfile:
        profile = existing or HouseholdProfile()
        profile.adults = self.counts["adults"]
        profile.children = self.counts["children"]
        profile.seniors = self.counts["seniors"]
        profile.vegetarian = bool(self.vegetarian.value)
        profile.no_pork = bool(self.no_pork.value)
        profile.lactose_free = bool(self.lactose_free.value)
        profile.allergies = list(self.allergies)
        profile.city = (self.city.value or "").strip()
        profile.zip_code = (self.zip_code.value or "").strip()
        return profile
