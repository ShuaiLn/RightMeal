"""Profile page: edit the household profile, API keys, and saved data."""

from __future__ import annotations

from typing import Callable

import flet as ft

import theme
from models import HouseholdProfile
from models.profile import API_KEY_NAMES
from services.keys import resolve_key
from ui.components import muted_text, primary_button, section_card, style_field
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

    key_fields = {}
    for name in API_KEY_NAMES:
        field = ft.TextField(
            label=API_KEY_FIELD_LABELS[name],
            value=profile.api_keys.get(name, ""),
            password=True,
            can_reveal_password=True,
            width=340,
        )
        style_field(field)
        key_fields[name] = field
    status_line = muted_text(_provider_status(profile), size=12)

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

    def show_clear_all_confirmation(e):
        def confirm_clear(ev):
            page.pop_dialog()
            on_delete()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Clear all user data?"),
            content=ft.Container(
                width=460,
                content=ft.Column(
                    [
                        ft.Text(
                            "This permanently removes your household profile and API keys, "
                            "current plan, pantry, leftovers, purchase history, and imported "
                            "or purchase photos from this computer."
                        ),
                        ft.Text(
                            "This action cannot be undone.",
                            color=theme.DANGER,
                            weight=ft.FontWeight.W_700,
                        ),
                    ],
                    spacing=10,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
                ft.FilledButton(
                    content="Clear all user data",
                    icon=ft.Icons.DELETE_FOREVER_OUTLINED,
                    on_click=confirm_clear,
                    style=ft.ButtonStyle(
                        bgcolor=theme.DANGER,
                        color=theme.SURFACE,
                        icon_color=theme.SURFACE,
                        shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                    ),
                ),
            ],
        ))

    save_button = primary_button("Save profile", icon=ft.Icons.CHECK)
    save_button.on_click = save

    clear_all_button = ft.OutlinedButton(
        content="Clear all user data",
        icon=ft.Icons.DELETE_FOREVER_OUTLINED,
        on_click=show_clear_all_confirmation,
        tooltip="Permanently remove all locally saved RightMeal user data",
        style=ft.ButtonStyle(
            color=theme.DANGER,
            icon_color=theme.DANGER,
            side=ft.BorderSide(1, theme.BORDER),
            shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
        ),
        height=44,
    )

    data_card = section_card(
        "Data and privacy",
        muted_text(
            "Remove this household and all of its locally saved plans, pantry data, "
            "history, leftovers, API keys, and user photos.",
            size=13,
        ),
        clear_all_button,
        subtitle="RightMeal stores this information only on this computer.",
    )

    keys_card = section_card(
        "API keys",
        *key_fields.values(),
        status_line,
        subtitle=(
            "All keys are optional — without them RightMeal uses BLS averages and seed "
            "estimates. Leave a field blank to use the matching environment variable "
            "from your .env file. Keys entered here are saved in your local profile."
        ),
    )

    return ft.Column(
        [
            ft.Text("Your household profile", size=24, weight=ft.FontWeight.W_700, color=theme.TEXT),
            muted_text("Everything here stays on this computer.", size=14),
            form.build(),
            keys_card,
            ft.Row([save_button], spacing=12),
            data_card,
        ],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
