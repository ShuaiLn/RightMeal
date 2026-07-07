"""App shell: theme, navigation, and view switching."""

from __future__ import annotations

import flet as ft

import theme
from models import HouseholdProfile
from services.profile_store import ProfileStore
from ui.onboarding_view import build_onboarding_view
from ui.planning_view import build_planning_view
from ui.profile_view import build_profile_view
from ui.state import AppState


def main(page: ft.Page):
    page.title = "RightMeal"
    page.bgcolor = theme.BG_WHITE
    page.theme = ft.Theme(color_scheme_seed=theme.DEEP_GREEN)
    page.padding = 20

    state = AppState(store=ProfileStore())
    state.profile = state.store.load()

    content = ft.Container(expand=True)
    plan_nav = ft.TextButton(content="Plan", icon=ft.Icons.SHOPPING_BASKET)
    profile_nav = ft.TextButton(content="Profile", icon=ft.Icons.PERSON)

    header = ft.Row(
        [
            ft.Icon(ft.Icons.ECO, color=theme.DEEP_GREEN),
            ft.Text("RightMeal", size=22, weight=ft.FontWeight.BOLD, color=theme.TEXT_DARK),
            ft.Text("affordable, balanced groceries", size=13, color=theme.TEXT_MUTED),
            ft.Container(expand=True),
            plan_nav,
            profile_nav,
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=10,
    )

    def set_nav_enabled(enabled: bool) -> None:
        plan_nav.disabled = not enabled
        profile_nav.disabled = not enabled

    def show_onboarding() -> None:
        set_nav_enabled(False)
        content.content = build_onboarding_view(page, on_save=handle_first_save)
        page.update()

    def show_planning(e=None) -> None:
        set_nav_enabled(True)
        content.content = build_planning_view(page, state)
        page.update()

    def show_profile(e=None) -> None:
        set_nav_enabled(True)
        content.content = build_profile_view(
            page, state.profile, on_save=handle_profile_save, on_delete=handle_delete
        )
        page.update()

    def handle_first_save(profile: HouseholdProfile) -> None:
        state.profile = profile
        state.store.save(profile)
        show_planning()

    def handle_profile_save(profile: HouseholdProfile) -> None:
        state.profile = profile
        state.store.save(profile)
        show_planning()

    def handle_delete() -> None:
        state.store.delete()
        state.profile = None
        state.cache.clear()
        show_onboarding()

    plan_nav.on_click = show_planning
    profile_nav.on_click = show_profile

    page.add(
        ft.SafeArea(
            expand=True,
            content=ft.Column(
                [header, ft.Divider(color=theme.BORDER_GREEN), content],
                spacing=10,
                expand=True,
            ),
        )
    )

    if state.profile is None:
        show_onboarding()
    else:
        show_planning()
