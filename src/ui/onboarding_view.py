"""First-run onboarding: collect the household profile and save it locally."""

from __future__ import annotations

from typing import Callable

import flet as ft

import theme
from models import HouseholdProfile
from ui.components import muted_text, primary_button
from ui.household_form import HouseholdForm


def build_onboarding_view(
    page: ft.Page,
    on_save: Callable[[HouseholdProfile], None],
) -> ft.Control:
    form = HouseholdForm(page, profile=None)

    def save(e):
        if not form.validate():
            return
        on_save(form.read_profile())

    save_button = primary_button("Save and start planning", icon=ft.Icons.ARROW_FORWARD)
    save_button.on_click = save

    return ft.Column(
        [
            ft.Text("Welcome to RightMeal", size=24, weight=ft.FontWeight.W_700, color=theme.TEXT),
            muted_text(
                "Tell us about your household once — then plan affordable, balanced "
                "grocery baskets any time. You can edit everything later on the Profile page.",
                size=14,
            ),
            form.build(),
            ft.Row([save_button]),
        ],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
