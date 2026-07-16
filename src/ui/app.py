"""App shell: theme, navigation, and view switching."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

import flet as ft

import theme
from models import HouseholdProfile, Pantry
from services.pantry_flow import migrate_legacy_purchases, rebuild_purchase_aggregates
from services.profile_store import ProfileStore
from services.purchase_log_store import sweep_orphan_photos
from services.photo_import_store import sweep_orphan_imported_images
from services.source_allocation import is_historical
from ui.calendar_view import build_calendar_view
from ui.onboarding_view import build_onboarding_view
from ui.pantry_view import build_pantry_view
from ui.planning_view import build_planning_view
from ui.profile_view import build_profile_view
from ui.start_view import build_start_view
from ui.state import AppState


WINDOW_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "icon_windows.ico"


def _plan_image_urls(state: AppState) -> list[str]:
    """Every food photo the saved plan could show: basket cards and every
    meal portion (covering the meal-card ingredient-photo fallback too)."""
    plan = state.saved_plan
    if plan is None:
        return []
    urls: set[str] = set()
    for item in plan.basket:
        food = state.foods_by_id.get(item.food_id)
        if food is not None and food.image_url:
            urls.add(food.image_url)
    for day in plan.meal_plan.days:
        for meal in day.meals:
            for portion in meal.portions:
                if portion.food.image_url:
                    urls.add(portion.food.image_url)
    return sorted(urls)


class NavPill:
    """A rounded navigation button with an explicit active state."""

    def __init__(self, label: str, icon: str, on_click):
        self._icon = ft.Icon(icon, size=16, color=theme.TEXT_MUTED)
        self._label = ft.Text(label, size=13, weight=ft.FontWeight.W_600, color=theme.TEXT_MUTED)
        self.control = ft.Container(
            content=ft.Row([self._icon, self._label], spacing=6),
            padding=ft.Padding.symmetric(horizontal=14, vertical=8),
            border_radius=999,
            ink=True,
            on_click=on_click,
        )

    def set_active(self, active: bool) -> None:
        color = theme.PRIMARY_DARK if active else theme.TEXT_MUTED
        self.control.bgcolor = theme.PRIMARY_TINT if active else None
        self._icon.color = color
        self._label.color = color


def main(page: ft.Page):
    page.title = "RightMeal"
    page.window.icon = str(WINDOW_ICON_PATH)
    page.bgcolor = theme.BG
    page.theme = ft.Theme(color_scheme_seed=theme.PRIMARY)
    page.padding = 0

    state = AppState(store=ProfileStore())
    # Roll back any save interrupted mid-transaction BEFORE loading the stores.
    recovery_warning = state.tx.recover_pending()
    state.profile = state.store.load()
    state.saved_plan = state.plan_store.load(state.foods_by_id)
    state.pantry = state.pantry_store.load(state.foods_by_id)
    state.prepared_leftovers = state.prepared_leftovers_store.load(state.foods_by_id)
    state.recipes = state.recipe_store.load()
    load_result = state.purchase_log_store.load()
    state.purchase_log = load_result.records
    state.purchase_log_error = load_result.load_error
    photo_load_result = state.photo_import_store.load()
    state.photo_imports = photo_load_result.records
    state.photo_import_error = photo_load_result.load_error
    if state.purchase_log_error is None:
        # One-time migrations (idempotent — deterministic ids make retries
        # safe): backfill plan_id on v2 plans and convert legacy purchased
        # entries into synthetic records. Aggregates rebuild for the LIVE
        # plan only; historical plans keep their frozen snapshot. Both are
        # skipped entirely when the log failed to load.
        migrated = []
        if state.saved_plan is not None:
            migrated = migrate_legacy_purchases(state.saved_plan, state.purchase_log)
            if not is_historical(state.saved_plan):
                rebuild_purchase_aggregates(state.saved_plan, state.purchase_log)
        plan_needs_resave = bool(
            state.saved_plan is not None
            and (migrated or state.saved_plan.needs_resave)
        )
        if plan_needs_resave or load_result.needs_resave:
            try:
                state.persist(
                    plan=(state.saved_plan if plan_needs_resave else None),
                    purchases=state.purchase_log,
                )
                if state.saved_plan is not None and plan_needs_resave:
                    state.saved_plan.needs_resave = False
            except Exception:  # noqa: BLE001 - retried next launch (idempotent)
                pass
        sweep_orphan_photos(state.purchase_log_store, state.purchase_log)
        if state.photo_import_error is None:
            sweep_orphan_imported_images(
                state.photo_import_store,
                state.photo_imports,
                state.purchase_log,
                state.pantry.custom_items,
            )

    DEFAULT_CONTENT_PADDING = ft.Padding.symmetric(horizontal=24, vertical=20)
    PLAN_CONTENT_PADDING = ft.Padding.only(left=24, right=8, top=20, bottom=20)

    content = ft.Container(expand=True, padding=DEFAULT_CONTENT_PADDING)
    # Tracks whichever show_* function is currently active, so the image-cache
    # warm-up below can re-render the live view once photos are cached —
    # without hardcoding which view that is (the user may navigate before it
    # finishes, or open Calendar before ever visiting Plan).
    current_view: dict[str, Callable[[], None]] = {}

    start_nav = NavPill("Start", ft.Icons.ROCKET_LAUNCH_OUTLINED, lambda e: show_start())
    plan_nav = NavPill("Plan", ft.Icons.SHOPPING_BASKET_OUTLINED, lambda e: show_planning())
    pantry_nav = NavPill("Pantry", ft.Icons.KITCHEN_OUTLINED, lambda e: show_pantry())
    calendar_nav = NavPill("Calendar", ft.Icons.CALENDAR_MONTH_OUTLINED, lambda e: show_calendar())
    profile_nav = NavPill("Profile", ft.Icons.PERSON_OUTLINE, lambda e: show_profile())
    nav_row = ft.Row(
        [
            start_nav.control,
            plan_nav.control,
            pantry_nav.control,
            calendar_nav.control,
            profile_nav.control,
        ],
        spacing=4,
    )

    brand_mark = ft.Container(
        content=ft.Image(
            src="/RightMeal logo.png",
            width=34,
            height=34,
            fit=ft.BoxFit.CONTAIN,
        ),
        width=34,
        height=34,
        alignment=ft.Alignment.CENTER,
    )
    header = ft.Container(
        content=ft.Row(
            [
                brand_mark,
                ft.Text("RightMeal", size=17, weight=ft.FontWeight.W_700, color=theme.TEXT),
                ft.Container(expand=True),
                nav_row,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10,
        ),
        bgcolor=theme.SURFACE,
        padding=ft.Padding.symmetric(horizontal=24, vertical=12),
        border=ft.Border(bottom=ft.BorderSide(1, theme.BORDER)),
    )

    def set_active_nav(active: NavPill | None) -> None:
        nav_row.visible = active is not None
        for nav in (start_nav, plan_nav, pantry_nav, calendar_nav, profile_nav):
            nav.set_active(nav is active)

    def show_onboarding() -> None:
        set_active_nav(None)
        content.padding = DEFAULT_CONTENT_PADDING
        content.content = build_onboarding_view(page, on_save=handle_first_save)
        page.update()
        current_view["refresh"] = show_onboarding

    def show_start(e=None) -> None:
        set_active_nav(start_nav)
        content.padding = DEFAULT_CONTENT_PADDING
        content.content = build_start_view(
            page,
            state,
            on_planned=show_planning,
            on_edit_household=show_profile,
            on_delete_plan=handle_plan_delete,
        )
        page.update()
        current_view["refresh"] = show_start

    def show_planning(e=None, initial_date: date | None = None) -> None:
        set_active_nav(plan_nav)
        content.padding = PLAN_CONTENT_PADDING
        content.content = build_planning_view(
            page, state, on_go_to_start=show_start, initial_date=initial_date
        )
        page.update()
        current_view["refresh"] = show_planning

    def show_pantry(e=None) -> None:
        set_active_nav(pantry_nav)
        content.padding = DEFAULT_CONTENT_PADDING
        content.content = build_pantry_view(page, state)
        page.update()
        current_view["refresh"] = show_pantry

    def show_calendar(e=None) -> None:
        set_active_nav(calendar_nav)
        content.padding = DEFAULT_CONTENT_PADDING
        content.content = build_calendar_view(
            page,
            state,
            on_go_to_plan=show_start,
            on_open_in_plan=lambda when: show_planning(initial_date=when),
        )
        page.update()
        current_view["refresh"] = show_calendar

    def show_profile(e=None) -> None:
        set_active_nav(profile_nav)
        content.padding = DEFAULT_CONTENT_PADDING
        content.content = build_profile_view(
            page, state.profile, on_save=handle_profile_save, on_delete=handle_delete
        )
        page.update()
        current_view["refresh"] = show_profile

    def handle_first_save(profile: HouseholdProfile) -> None:
        state.profile = profile
        state.store.save(profile)
        state.begin_generation()  # a profile save bypasses persist(); bump here
        show_start()

    def handle_profile_save(profile: HouseholdProfile) -> None:
        state.profile = profile
        state.store.save(profile)
        # The profile is a generation input: an in-flight generation started
        # before this edit must never commit its (now stale) result.
        state.begin_generation()
        if state.saved_plan is not None:
            show_planning()
        else:
            show_start()

    def handle_plan_delete() -> None:
        # Invalidate any generation still running before removing its target.
        state.begin_generation()
        try:
            state.plan_store.delete()
        except OSError as exc:
            page.show_dialog(ft.SnackBar(ft.Text(f"Could not delete the plan: {exc}")))
            return
        state.saved_plan = None
        state.plan_revision += 1
        show_start()

    def handle_delete() -> None:
        # Clearing user data must also invalidate work that captured the old
        # profile or stores and could otherwise write it back after deletion.
        state.begin_generation()
        state.begin_photo_analysis()
        state.store.delete()
        state.plan_store.delete()
        state.pantry_store.delete()
        state.prepared_leftovers_store.delete()
        state.recipe_store.delete()
        state.purchase_log_store.delete()
        state.photo_import_store.delete()
        for stray in (
            state.tx.journal_path,
            state.store.base_dir / "ai_consent.json",
            *state.store.base_dir.glob("tx_journal.corrupt-*.json"),
        ):
            try:
                stray.unlink()
            except OSError:
                pass
        state.profile = None
        state.saved_plan = None
        state.pantry = Pantry()
        state.prepared_leftovers = []
        state.recipes = {}
        state.purchase_log = []
        state.purchase_log_error = None
        state.photo_imports = []
        state.photo_import_error = None
        state.cache.clear()
        show_onboarding()

    page.add(
        ft.SafeArea(
            expand=True,
            content=ft.Column([header, content], spacing=0, expand=True),
        )
    )

    image_urls = _plan_image_urls(state)
    if image_urls:
        async def warm_plan_images() -> None:
            await state.image_cache.prefetch(image_urls)
            refresh = current_view.get("refresh")
            if refresh is not None:
                refresh()

        page.run_task(warm_plan_images)

    if state.profile is None:
        show_onboarding()
    elif state.saved_plan is not None:
        show_planning()
    else:
        show_start()

    if recovery_warning:
        page.show_dialog(ft.SnackBar(ft.Text(recovery_warning)))
    elif state.purchase_log_error:
        page.show_dialog(ft.SnackBar(ft.Text(
            "Purchase history could not be read — purchasing is paused to "
            "protect your data."
        )))
