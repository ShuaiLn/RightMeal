"""Start page: budget, location, and plan dates — builds and saves the plan."""

from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, timedelta
from typing import Callable

import flet as ft

import theme
from data import load_bls_price_map
from models import (
    HouseholdProfile,
    Location,
    SavedBasketItem,
    SavedPlan,
    SavedUnusedFood,
)
from planner import (
    assign_prepared_leftovers,
    categorize_unused,
    consumed_gaps,
    household_slot_targets,
)
from planner.recipe_scheduler import PlanGenerationError
from services.planner_engine import (
    collect_staples, generate_recipe_first, parse_variety_mode,
)
from services.explanation import get_explanation_service
from services.keys import resolve_key
from services.price_engine import PriceEngine
from services.price_providers import (
    BlsProvider,
    InstacartProvider,
    KrogerProvider,
    SeedProvider,
)
from ui.components import muted_text, primary_button, section_card, style_field
from ui.date_range_picker import range_label, show_range_picker
from ui.household_form import ZIP_RE
from ui.plan_draft import (
    PLAN_MODES,
    household_summary_text,
    restore_draft_values,
    snapshot_draft,
)
from ui.state import AppState

_PLAN_MODE_LABELS = {"weekly": "Weekly", "daily": "Daily"}


def build_engine(state: AppState) -> PriceEngine:
    profile = state.profile
    client = state.http_client
    providers = [
        KrogerProvider(
            resolve_key("kroger_client_id", profile),
            resolve_key("kroger_client_secret", profile),
            client,
        ),
        InstacartProvider(resolve_key("instacart_api_key", profile), client),
        BlsProvider(load_bls_price_map(), client, resolve_key("bls_api_key", profile)),
        SeedProvider(),
    ]
    return PriceEngine(providers, state.cache)


def build_start_view(
    page: ft.Page,
    state: AppState,
    on_planned: Callable[[], None],
    on_edit_household: Callable[[], None],
) -> ft.Control:
    profile: HouseholdProfile = state.profile

    if state.plan_draft is not None:
        budget_text, zip_text, mode_value, initial_variety, start_date, end_date = (
            restore_draft_values(state.plan_draft, profile, date.today())
        )
    else:
        budget_text = "50"
        zip_text = profile.zip_code
        mode_value = "weekly"
        initial_variety = getattr(profile, "variety_mode", "balanced") or "balanced"
        start_date, end_date = date.today(), date.today() + timedelta(days=6)

    budget_field = ft.TextField(
        label="Budget (USD)", value=budget_text, width=150, keyboard_type=ft.KeyboardType.NUMBER
    )
    mode_button = ft.SegmentedButton(
        segments=[ft.Segment(value=m, label=_PLAN_MODE_LABELS[m]) for m in PLAN_MODES],
        selected=[mode_value],
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM)),
    )
    zip_field = ft.TextField(label="ZIP code", value=zip_text, width=130)
    for field in (budget_field, zip_field):
        style_field(field)

    variety_button = ft.SegmentedButton(
        segments=[
            ft.Segment(value="high_variety", label=ft.Text("High variety")),
            ft.Segment(value="balanced", label=ft.Text("Balanced")),
            ft.Segment(value="meal_prep", label=ft.Text("Meal prep")),
        ],
        selected=[initial_variety],
        allow_multiple_selection=False,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM)),
    )

    dates = {"start": start_date, "end": end_date}

    def on_edit_click(e) -> None:
        state.plan_draft = snapshot_draft(
            budget_field.value or "",
            zip_field.value or "",
            profile.zip_code,
            (mode_button.selected or ["weekly"])[0],
            (variety_button.selected or ["balanced"])[0],
            dates["start"],
            dates["end"],
        )
        on_edit_household()

    edit_button = ft.TextButton("Edit household", on_click=on_edit_click)

    def horizon_days() -> int:
        return (dates["end"] - dates["start"]).days + 1

    range_button = ft.TextButton(
        icon=ft.Icons.CALENDAR_MONTH,
        tooltip="The days this plan covers — pick a first and a last day",
    )

    def update_range_button() -> None:
        range_button.content = range_label(dates["start"], dates["end"])

    def on_range_picked(picked_start: date, picked_end: date) -> None:
        dates["start"], dates["end"] = picked_start, picked_end
        update_range_button()
        update_caption()

    range_button.on_click = lambda e: show_range_picker(
        page, dates["start"], dates["end"], on_range_picked
    )
    update_range_button()

    caption = muted_text("")
    plan_button = primary_button("Plan my groceries", icon=ft.Icons.SHOPPING_BASKET_OUTLINED)

    members_ok = profile.total_members >= 1
    if members_ok:
        household_summary: ft.Control = muted_text(household_summary_text(profile), size=13)
    else:
        household_summary = ft.Text(
            "Add at least one household member to plan meals.",
            size=13, color=theme.WARN_INK,
        )
        plan_button.disabled = True

    progress_bar = ft.ProgressBar(
        value=0,
        color=theme.PRIMARY,
        bgcolor=theme.SURFACE_TINT,
        bar_height=6,
        border_radius=999,
        visible=False,
    )
    status_text = muted_text("")

    def parse_inputs() -> tuple[float, str] | None:
        ok = True
        try:
            amount = float(budget_field.value or "")
            if amount <= 0:
                raise ValueError
            budget_field.error = None
        except ValueError:
            budget_field.error = "Enter a positive amount"
            amount, ok = 0.0, False
        if not ZIP_RE.match(zip_field.value or ""):
            zip_field.error = "Enter a 5-digit U.S. ZIP code"
            ok = False
        else:
            zip_field.error = None
        return (amount, zip_field.value) if ok else None

    def planning_budget(amount: float, horizon: int) -> float:
        # Daily budgets scale by the horizon; weekly budgets scale by horizon/7.
        if "daily" in mode_button.selected:
            return amount * horizon
        return amount * horizon / 7.0

    def update_caption(e=None):
        try:
            amount = float(budget_field.value or "")
        except ValueError:
            caption.value = ""
            page.update()
            return
        horizon = horizon_days()
        caption.value = f"= ${planning_budget(amount, horizon):.2f} for {horizon} days"
        page.update()

    budget_field.on_change = update_caption
    mode_button.on_change = update_caption

    async def on_plan_click(e):
        parsed = parse_inputs()
        page.update()
        if parsed is None:
            return
        amount, zip_code = parsed
        horizon = horizon_days()
        budget = round(planning_budget(amount, horizon), 2)
        location = Location(city=profile.city, zip_code=zip_code)

        variety_value = (variety_button.selected or ["balanced"])[0]
        variety_mode = parse_variety_mode(variety_value)

        # Persist the sticky variety mode NOW, at click time: the live profile
        # is still fresh, this happens before the token below is issued (so a
        # profile-save bump can't invalidate this very generation), and before
        # any await (so it can't clobber a household edit made mid-flight).
        if getattr(profile, "variety_mode", None) != variety_value:
            profile.variety_mode = variety_value
            try:
                state.store.save(profile)
            except Exception:  # noqa: BLE001 - best effort, non-fatal
                pass

        gen_id = state.begin_generation()

        # Immutable snapshots of every mutable input, taken before the first
        # await. Everything past this point reads ONLY the snapshots — never
        # state.pantry, state.prepared_leftovers, or the live profile — so a
        # half-edited configuration can never leak into the plan. The catalogs
        # (state.foods / foods_by_id / recipes_catalog) are loaded once at
        # startup and never mutated; they are relied on as immutable.
        profile_snapshot = copy.deepcopy(profile)
        pantry_snapshot = dict(state.pantry.items)
        leftovers_snapshot = tuple(state.prepared_leftovers)

        plan_button.disabled = True
        progress_bar.visible = True
        progress_bar.value = 0
        status_text.value = "Looking up prices…"
        page.update()

        def on_progress(done: int, total: int):
            progress_bar.value = done / total
            status_text.value = f"Pricing {done} of {total} foods…"
            page.update()

        planned = False
        try:
            engine = build_engine(state)
            quotes = await engine.price_all(list(state.foods), location, on_progress=on_progress)
            if not state.is_current_generation(gen_id):
                return  # inputs changed while pricing — discard everything

            status_text.value = "Choosing recipes…"
            page.update()
            # Prepared leftovers are ready meals, never raw inventory: they pin
            # meal slots and reduce purchase demand, but stay out of the pantry
            # snapshot. The store is only read — servings are consumed when the
            # meal is eaten, so regenerating never uses anything up.
            assignments = assign_prepared_leftovers(
                leftovers_snapshot,
                horizon,
                dates["start"],
                state.foods_by_id,
                household_slot_targets(state.nutrition, profile_snapshot),
            )

            # Off the event loop: generation is CPU-bound and the UI must stay
            # responsive (progress text animating) while it runs.
            output = await asyncio.to_thread(
                generate_recipe_first,
                state.recipes_catalog, state.foods_by_id, profile_snapshot,
                state.nutrition, pantry_snapshot, quotes, budget, horizon,
                variety_mode, preassigned=assignments,
            )
            if not state.is_current_generation(gen_id):
                return  # stale generation — its result must never be shown
            plan, result, staples = output.meal_plan, output.result, output.staples

            status_text.value = "Writing the explanation…"
            page.update()
            service = get_explanation_service(profile_snapshot, state.http_client)
            explanation = await service.explain(result, profile_snapshot)
            if not state.is_current_generation(gen_id):
                return  # stale generation — do not persist

            targets = state.nutrition.household_targets(profile_snapshot, horizon)
            unused = categorize_unused(state.foods, result, quotes)

            # All commits sit strictly after the last staleness check.
            saved = SavedPlan(
                start_date=dates["start"],
                horizon_days=horizon,
                created_at=datetime.now().isoformat(timespec="seconds"),
                budget=budget,
                total_cost=result.total_cost,
                meal_plan=plan,
                basket=tuple(
                    SavedBasketItem(
                        food_id=item.food.id,
                        package_label=item.package.label,
                        count=item.count,
                        cost=round(item.cost, 2),
                        source=item.quote.source.value,
                        store=item.quote.store,
                        confidence=item.quote.confidence,
                        match_reason=item.quote.match_reason,
                        matched_product_name=item.quote.matched_product_name,
                    )
                    for item in result.items
                ),
                consumed_gaps=tuple(consumed_gaps(plan, targets)),
                tracking={},
                pantry_used={
                    use.food.id: round(use.grams, 3) for use in result.pantry_used
                },
                leftovers_used={
                    a.leftover_id: round(a.servings_used, 4) for a in assignments
                },
                purchased_totals=result.nutrient_totals,
                explanation=explanation,
                nutrition_feasible=result.nutrition_feasible,
                budget_status=result.budget_status,
                relaxed_constraints=result.relaxed_constraints,
                dominance_flags=result.dominance_flags,
                unused=tuple(
                    SavedUnusedFood(category=category.value, food_id=food.id, reason=reason)
                    for category, entries in unused.items()
                    for food, reason in entries
                ),
                variety_mode=variety_value,
                staples=staples,
            )
            state.persist(plan=saved)
            state.saved_plan = saved
            state.plan_draft = None
            status_text.value = ""
            planned = True
        except PlanGenerationError as exc:
            reason = exc.reasons[0] if exc.reasons else str(exc)
            status_text.value = (
                f"{exc} Try adjusting your budget, variety, or calorie needs. ({reason})"
            )
        except Exception as exc:  # noqa: BLE001 - surface errors in the UI
            status_text.value = f"Something went wrong: {type(exc).__name__}: {exc}"
        finally:
            # A stale return also lands here; resetting the (possibly detached)
            # view's button and progress bar is harmless.
            plan_button.disabled = False
            progress_bar.visible = False
            page.update()
        if planned:
            on_planned()

    plan_button.on_click = on_plan_click

    update_caption()

    replaces_note: list[ft.Control] = []
    if state.saved_plan is not None:
        replaces_note.append(
            muted_text(
                f"You already have a plan for "
                f"{range_label(state.saved_plan.start_date, state.saved_plan.end_date)} — "
                "planning again replaces it.",
                size=12,
            )
        )

    inputs_card = section_card(
        "Plan your groceries",
        ft.Row(
            [budget_field, mode_button, zip_field, range_button, plan_button],
            wrap=True,
            spacing=10,
            run_spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        ft.Row(
            [household_summary, edit_button],
            spacing=4,
            wrap=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        ft.Row(
            [muted_text("Variety", size=13), variety_button],
            spacing=10,
            wrap=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        caption,
        *replaces_note,
        progress_bar,
        status_text,
        subtitle="Set a budget, dates, and a variety style — RightMeal builds real recipes around them.",
    )

    return ft.Column(
        [inputs_card],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
