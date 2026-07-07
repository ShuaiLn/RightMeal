"""First-run onboarding: collect the household profile and save it locally."""

from __future__ import annotations

from typing import Callable

import flet as ft

import theme
from models import HouseholdProfile
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

    return ft.Column(
        [
            ft.Text("Welcome to RightMeal", size=24, weight=ft.FontWeight.BOLD, color=theme.TEXT_DARK),
            ft.Text(
                "Tell us about your household once — then plan affordable, balanced "
                "grocery baskets any time. You can edit everything later on the Profile page.",
                size=14,
                color=theme.TEXT_MUTED,
            ),
            form.build(),
            ft.FilledButton(content="Save and start planning", on_click=save),
        ],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
