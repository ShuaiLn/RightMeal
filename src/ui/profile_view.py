"""Profile page: edit the household profile, API keys, and saved data."""

from __future__ import annotations

from typing import Callable

import flet as ft

import theme
from models import HouseholdProfile
from models.profile import API_KEY_NAMES
from services.keys import resolve_key
from ui.components import muted_text, section_card
from ui.household_form import HouseholdForm

API_KEY_FIELD_LABELS = {
    "kroger_client_id": "Kroger client ID",
    "kroger_client_secret": "Kroger client secret",
    "instacart_api_key": "Instacart API key",
    "fdc_api_key": "USDA FoodData Central API key",
    "openai_api_key": "OpenAI API key",
    "bls_api_key": "BLS API key",
}


def _provider_status(profile: HouseholdProfile) -> str:
    kroger = bool(
        resolve_key("kroger_client_id", profile) and resolve_key("kroger_client_secret", profile)
    )
    parts = [
        f"Kroger: {'configured' if kroger else 'not configured'}",
        f"Instacart: {'configured' if resolve_key('instacart_api_key', profile) else 'not configured'}",
        "BLS: available (key optional)",
        f"OpenAI explanations: {'configured' if resolve_key('openai_api_key', profile) else 'local templates'}",
    ]
    return " · ".join(parts)


def build_profile_view(
    page: ft.Page,
    profile: HouseholdProfile,
    on_save: Callable[[HouseholdProfile], None],
    on_delete: Callable[[], None],
) -> ft.Control:
    form = HouseholdForm(page, profile)

    key_fields = {
        name: ft.TextField(
            label=API_KEY_FIELD_LABELS[name],
            value=profile.api_keys.get(name, ""),
            password=True,
            can_reveal_password=True,
            width=340,
        )
        for name in API_KEY_NAMES
    }
    status_line = muted_text(_provider_status(profile))

    def save(e):
        if not form.validate():
            return
        updated = form.read_profile(existing=profile)
        updated.api_keys = {
            name: (field.value or "").strip()
            for name, field in key_fields.items()
            if (field.value or "").strip()
        }
        status_line.value = _provider_status(updated)
        on_save(updated)

    def delete(e):
        on_delete()

    keys_card = section_card(
        "API keys (optional)",
        muted_text(
            "All keys are optional — without them RightMeal uses BLS averages and seed "
            "estimates. Leave a field blank to use the matching environment variable "
            "from your .env file. Keys entered here are saved in your local profile."
        ),
        *key_fields.values(),
        status_line,
    )

    return ft.Column(
        [
            ft.Text("Your household profile", size=24, weight=ft.FontWeight.BOLD, color=theme.TEXT_DARK),
            form.build(),
            keys_card,
            ft.Row(
                [
                    ft.FilledButton(content="Save profile", on_click=save),
                    ft.OutlinedButton(
                        content="Delete saved data",
                        icon=ft.Icons.DELETE_OUTLINE,
                        on_click=delete,
                        tooltip="Removes profile.json from this computer and restarts onboarding",
                    ),
                ],
                spacing=12,
            ),
        ],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
