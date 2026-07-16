"""Start page: budget, location, and plan dates — builds and saves the plan."""

from __future__ import annotations

import asyncio
import copy
import math
from datetime import date, datetime, timedelta
from typing import Callable

import flet as ft

import theme
from data import load_bls_price_map
from models import (
    BudgetChoiceRequired,
    DailyFoodCoverage,
    DataUnavailable,
    HouseholdProfile,
    HouseholdSnapshot,
    Location,
    NoFeasiblePlanProven,
    NoPlanFoundWithinSearchLimits,
    PartialFoodCoverageCandidate,
    PlanKind,
    RequiredPriceUnavailable,
    SavedBasketItem,
    SavedPlan,
    SavedUnusedFood,
    StandardPlanReady,
    deterministic_basket_item_id,
)
from models.plan import new_plan_id
from planner import (
    assign_prepared_leftovers,
    categorize_unused,
    consumed_gaps,
    household_slot_targets,
)
from services.planner_engine import (
    collect_recipe_data_issues,
    generate_recipe_first,
    generate_recipe_first_outcome,
    parse_variety_mode,
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

ESTIMATED_CAP_DISCLAIMER = (
    "This cap applies to the current mixed-source basket estimate. It is not a "
    "checkout guarantee and excludes tax, delivery fees, minimum orders, "
    "membership terms, and later price changes."
)
PARTIAL_PLAN_WARNING = "This is not a complete food plan. Additional food is required."


def _daily_food_coverage(plan, profile, nutrition) -> tuple[DailyFoodCoverage, ...]:
    """Persist generation-time daily evidence for standard plans too."""

    targets = nutrition.household_daily_targets(profile)
    rows: list[DailyFoodCoverage] = []
    for day in plan.days:
        calories = 0.0
        protein = 0.0
        scales: list[float] = []
        for meal in day.meals:
            calories += meal.nutrients.calories_kcal
            protein += meal.nutrients.protein_g
            scales.append(float(meal.portion_scale))
        rows.append(DailyFoodCoverage(
            day_index=day.day_index,
            portion_scale=min(scales, default=1.0),
            calories_ratio=(calories / targets.calories_kcal if targets.calories_kcal > 0 else 1.0),
            protein_ratio=(protein / targets.protein_g if targets.protein_g > 0 else 1.0),
        ))
    return tuple(rows)


def _base_budget_for_cap(cap: float, horizon: int, *, daily_mode: bool) -> float:
    """Translate a whole-horizon cap back to the Start form's unit."""

    raw = cap / horizon if daily_mode else cap * 7.0 / horizon
    # The field displays cents. Round upward so accepting a candidate cannot
    # recreate a horizon cap one cent below that same candidate.
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


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
    on_delete_plan: Callable[[], None] | None = None,
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
        label="Estimated basket budget cap",
        value=budget_text,
        width=220,
        keyboard_type=ft.KeyboardType.NUMBER,
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

    delete_plan_button: ft.OutlinedButton | None = None
    if state.saved_plan is not None:
        def show_delete_plan_confirmation(e) -> None:
            def confirm_delete(ev) -> None:
                page.pop_dialog()
                if on_delete_plan is not None:
                    on_delete_plan()

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Delete current plan?"),
                content=ft.Text(
                    "This removes the saved plan and its meal schedule. Your pantry, "
                    "purchase history, household profile, and prepared leftovers will be kept."
                ),
                actions=[
                    ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
                    ft.FilledButton(
                        content="Delete plan",
                        icon=ft.Icons.DELETE_OUTLINE,
                        on_click=confirm_delete,
                        style=ft.ButtonStyle(
                            bgcolor=theme.DANGER,
                            color=theme.SURFACE,
                            icon_color=theme.SURFACE,
                            shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                        ),
                    ),
                ],
            ))

        delete_plan_button = ft.OutlinedButton(
            content="Delete plan",
            icon=ft.Icons.DELETE_OUTLINE,
            on_click=show_delete_plan_confirmation,
            tooltip="Delete the current saved plan",
            style=ft.ButtonStyle(
                color=theme.DANGER,
                icon_color=theme.DANGER,
                side=ft.BorderSide(1, theme.BORDER),
                shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
            ),
            height=44,
        )

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
        """Generate a truthful typed outcome and persist only a valid candidate."""

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

        if getattr(profile, "variety_mode", None) != variety_value:
            profile.variety_mode = variety_value
            try:
                state.store.save(profile)
            except Exception:  # noqa: BLE001 - best effort, non-fatal
                pass

        gen_id = state.begin_generation()
        profile_snapshot = copy.deepcopy(profile)
        pantry_snapshot = dict(state.pantry.items)
        leftovers_snapshot = tuple(state.prepared_leftovers)
        plan_button.disabled = True
        progress_bar.visible = True
        progress_bar.value = 0
        status_text.value = "Looking up package offers..."
        page.update()

        progress_phase = {"label": "Pricing"}

        def on_progress(done: int, total: int):
            progress_bar.value = done / total if total else 1
            status_text.value = f"{progress_phase['label']} {done} of {total} foods..."
            page.update()

        async def persist_candidate(outcome) -> bool:
            if not state.is_current_generation(gen_id):
                return False
            output = outcome.candidate
            plan, result, staples = output.meal_plan, output.result, output.staples
            # Saveability is intentionally rechecked at the last boundary.
            if result.total_cost_cents > round(budget * 100) or result.unpriced_food_ids:
                raise ValueError("The candidate no longer fits the estimated cap.")

            status_text.value = "Writing the explanation..."
            progress_bar.visible = True
            page.update()
            service = get_explanation_service(profile_snapshot, state.http_client)
            explanation = await service.explain(result, profile_snapshot)
            if not state.is_current_generation(gen_id):
                return False

            targets = state.nutrition.household_targets(profile_snapshot, horizon)
            unused = categorize_unused(state.foods, result, pricing)
            plan_id = new_plan_id()
            basket_rows: list[SavedBasketItem] = []
            for item in result.items:
                offer = item.offer
                source = offer.source if offer is not None else item.quote.source
                store = offer.store if offer is not None else item.quote.store
                confidence = offer.confidence if offer is not None else item.quote.confidence
                match_reason = (
                    offer.match_reason if offer is not None else item.quote.match_reason
                )
                matched_name = (
                    offer.matched_product_name
                    if offer is not None else item.quote.matched_product_name
                )
                basket_item_id = deterministic_basket_item_id(
                    plan_id,
                    item.food.id,
                    item.package_id,
                    item.offer_id,
                    item.count,
                )
                basket_rows.append(SavedBasketItem(
                    basket_item_id=basket_item_id,
                    food_id=item.food.id,
                    package_id=item.package_id,
                    package_label=item.package.label,
                    package_grams=float(item.package.grams),
                    count=item.count,
                    offer_id=item.offer_id,
                    unit_cost_cents=item.unit_cost_cents,
                    total_cost_cents=item.total_cost_cents,
                    source=source.value,
                    store=store,
                    confidence=confidence,
                    match_reason=match_reason,
                    matched_product_name=matched_name,
                ))

            is_partial = isinstance(outcome, PartialFoodCoverageCandidate)
            daily_coverage = (
                tuple(outcome.daily_coverage)
                if is_partial else _daily_food_coverage(plan, profile_snapshot, state.nutrition)
            )
            saved = SavedPlan(
                plan_id=plan_id,
                start_date=dates["start"],
                horizon_days=horizon,
                created_at=datetime.now().isoformat(timespec="seconds"),
                budget=budget,
                total_cost=result.total_cost_cents / 100.0,
                meal_plan=plan,
                basket=tuple(basket_rows),
                consumed_gaps=tuple(consumed_gaps(plan, targets)),
                tracking={},
                pantry_used={
                    use.food.id: round(use.grams, 3) for use in result.pantry_used
                },
                leftovers_used={
                    assignment.leftover_id: round(assignment.servings_used, 4)
                    for assignment in assignments
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
                plan_kind=(
                    PlanKind.PARTIAL_FOOD_COVERAGE if is_partial else PlanKind.STANDARD
                ),
                household_snapshot=HouseholdSnapshot.from_profile(profile_snapshot),
                daily_coverage=daily_coverage,
            )
            if not state.is_current_generation(gen_id):
                return False
            state.persist(plan=saved)
            state.saved_plan = saved
            state.plan_draft = None
            status_text.value = ""
            return True

        def show_budget_choice(outcome: BudgetChoiceRequired) -> None:
            candidate_total = outcome.estimated_total_cents / 100.0

            def set_cap(ev) -> None:
                budget_field.value = f"{_base_budget_for_cap(candidate_total, horizon, daily_mode='daily' in mode_button.selected):.2f}"
                page.pop_dialog()
                update_caption()
                page.update()

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Complete candidate is above the estimated cap"),
                content=ft.Container(
                    width=440,
                    content=ft.Column([
                        ft.Text(
                            "No complete plan was found within the current search limits "
                            "for this estimated cap."
                        ),
                        ft.Text(
                            f"One complete candidate found is currently estimated at "
                            f"${candidate_total:.2f}."
                        ),
                        muted_text(
                            "This amount is neither a proven minimum required cap nor a "
                            "single-store checkout price. " + ESTIMATED_CAP_DISCLAIMER,
                            size=12,
                        ),
                    ], spacing=10, tight=True),
                ),
                actions=[
                    ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
                    ft.FilledButton(
                        content=f"Set cap to current ${candidate_total:.2f} candidate",
                        on_click=set_cap,
                    ),
                ],
            ))

        def partial_summary(outcome: PartialFoodCoverageCandidate) -> ft.Column:
            daily = ", ".join(
                f"Day {index + 1}: {scale * 100:.0f}%"
                for index, scale in enumerate(outcome.daily_portion_scales)
            )
            return ft.Column([
                ft.Text(PARTIAL_PLAN_WARNING, color=theme.DANGER),
                ft.Text(f"Current estimate: ${outcome.estimated_total_cents / 100:.2f}"),
                ft.Text(f"Estimated budget cap: ${outcome.estimated_cap_cents / 100:.2f}"),
                ft.Text(f"Remaining budget: ${outcome.remaining_budget_cents / 100:.2f}"),
                ft.Text(f"Household members: {outcome.household_member_count}"),
                ft.Text(f"Daily portions: {daily}"),
                ft.Text(
                    "Minimum daily coverage: "
                    f"{outcome.minimum_daily_calorie_coverage * 100:.0f}% calories, "
                    f"{outcome.minimum_daily_protein_coverage * 100:.0f}% protein"
                ),
                ft.Text(
                    (
                        "Cheapest next 1% step would make the basket "
                        f"${outcome.next_increment_total_cents / 100:.2f} "
                        f"(${outcome.next_increment_additional_cents / 100:.2f} more)."
                    )
                    if outcome.next_increment_total_cents is not None
                    else "Every day's portions have reached 100%.",
                ),
                muted_text(
                    "Whole-package price steps can leave a small amount of the "
                    "budget that cannot fund another 1% portion increase.",
                    size=12,
                ),
                muted_text(ESTIMATED_CAP_DISCLAIMER, size=12),
            ], spacing=8, tight=True)

        def show_partial_confirmation(outcome: PartialFoodCoverageCandidate) -> None:
            async def save_partial(ev) -> None:
                page.pop_dialog()
                plan_button.disabled = True
                progress_bar.visible = True
                try:
                    saved = await persist_candidate(outcome)
                except Exception as exc:  # noqa: BLE001 - surface persistence failures
                    status_text.value = f"Something went wrong: {type(exc).__name__}: {exc}"
                    saved = False
                finally:
                    plan_button.disabled = False
                    progress_bar.visible = False
                    page.update()
                if saved:
                    on_planned()

            def show_final_warning(ev) -> None:
                page.pop_dialog()
                page.show_dialog(ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Confirm incomplete food coverage"),
                    content=ft.Container(
                        width=480,
                        content=ft.Column([
                            partial_summary(outcome),
                            ft.Divider(height=1),
                            ft.Text(
                                PARTIAL_PLAN_WARNING,
                                weight=ft.FontWeight.W_700,
                                color=theme.DANGER,
                            ),
                        ], spacing=10, tight=True),
                    ),
                    actions=[
                        ft.TextButton(content="Cancel", on_click=lambda event: page.pop_dialog()),
                        ft.FilledButton(content="Save partial plan", on_click=save_partial),
                    ],
                ))

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Review partial food coverage plan"),
                content=ft.Container(width=480, content=partial_summary(outcome)),
                actions=[
                    ft.TextButton(content="Cancel", on_click=lambda ev: page.pop_dialog()),
                    ft.FilledButton(content="Continue", on_click=show_final_warning),
                ],
            ))

        planned = False
        try:
            engine = build_engine(state)
            legacy_price_api = not hasattr(engine, "price_all_offers")
            if legacy_price_api:
                # Compatibility for staged/custom engines. The built-in engine
                # always uses package offers and the branch below.
                pricing = await engine.price_all(
                    list(state.foods), location, on_progress=on_progress
                )
                offer_book = None
            else:
                offer_book = await engine.price_all_offers(
                    list(state.foods), location, on_progress=on_progress
                )
                pricing = dict(offer_book.offers_by_food)
            if not state.is_current_generation(gen_id):
                return

            assignments = assign_prepared_leftovers(
                leftovers_snapshot,
                horizon,
                dates["start"],
                state.foods_by_id,
                household_slot_targets(state.nutrition, profile_snapshot),
            )
            status_text.value = "Choosing recipes..."
            page.update()
            if legacy_price_api:
                output = await asyncio.to_thread(
                    generate_recipe_first,
                    state.recipes_catalog,
                    state.foods_by_id,
                    profile_snapshot,
                    state.nutrition,
                    pantry_snapshot,
                    pricing,
                    budget,
                    horizon,
                    variety_mode,
                    preassigned=assignments,
                )
                cap_cents = round(budget * 100)
                issues = collect_recipe_data_issues(
                    state.recipes_catalog, state.foods_by_id, profile_snapshot, pricing
                )
                if output.result.unpriced_food_ids:
                    outcome = DataUnavailable(
                        issues=(RequiredPriceUnavailable(
                            affected_count=len(output.result.unpriced_food_ids),
                            food_ids=tuple(output.result.unpriced_food_ids),
                            detail="the selected candidate contains unpriced food",
                        ),),
                        reason="Required pricing data is unavailable.",
                    )
                elif output.result.total_cost_cents <= cap_cents:
                    outcome = StandardPlanReady(
                        candidate=output,
                        estimated_total_cents=output.result.total_cost_cents,
                        estimated_cap_cents=cap_cents,
                        data_issues=issues,
                    )
                else:
                    outcome = BudgetChoiceRequired(
                        candidate=output,
                        estimated_total_cents=output.result.total_cost_cents,
                        estimated_cap_cents=cap_cents,
                        data_issues=issues,
                    )
            else:
                outcome = await asyncio.to_thread(
                    generate_recipe_first_outcome,
                    state.recipes_catalog,
                    state.foods_by_id,
                    profile_snapshot,
                    state.nutrition,
                    pantry_snapshot,
                    pricing,
                    budget,
                    horizon,
                    variety_mode,
                    preassigned=assignments,
                )
            if not state.is_current_generation(gen_id):
                return

            # Seed estimates are a last resort only for foods whose absent price
            # made an otherwise-eligible plan impossible to evaluate.
            if isinstance(outcome, DataUnavailable) and offer_book is not None:
                fallback_ids = sorted({
                    food_id
                    for issue in outcome.issues
                    if isinstance(issue, RequiredPriceUnavailable)
                    for food_id in issue.food_ids
                    if food_id in offer_book.missing_food_ids
                })
                if fallback_ids:
                    progress_phase["label"] = "Trying local fallback for"
                    status_text.value = "Trying local estimates for required missing foods..."
                    page.update()
                    offer_book = await engine.price_all_offers(
                        list(state.foods),
                        location,
                        local_fallback_food_ids=fallback_ids,
                        on_progress=on_progress,
                    )
                    if not state.is_current_generation(gen_id):
                        return
                    pricing = dict(offer_book.offers_by_food)
                    outcome = await asyncio.to_thread(
                        generate_recipe_first_outcome,
                        state.recipes_catalog,
                        state.foods_by_id,
                        profile_snapshot,
                        state.nutrition,
                        pantry_snapshot,
                        pricing,
                        budget,
                        horizon,
                        variety_mode,
                        preassigned=assignments,
                    )
                    if not state.is_current_generation(gen_id):
                        return

            if isinstance(outcome, StandardPlanReady):
                planned = await persist_candidate(outcome)
            elif isinstance(outcome, PartialFoodCoverageCandidate):
                status_text.value = ""
                show_partial_confirmation(outcome)
            elif isinstance(outcome, BudgetChoiceRequired):
                status_text.value = ""
                show_budget_choice(outcome)
            elif isinstance(outcome, DataUnavailable):
                issue_summary = ", ".join(
                    f"{type(issue).__name__} ({issue.affected_count})"
                    for issue in outcome.issues
                )
                preservation = (
                    "Your existing plan was kept."
                    if state.saved_plan is not None
                    else "No plan was saved."
                )
                status_text.value = (
                    f"Required planning data is unavailable. {preservation} "
                    f"{issue_summary}."
                )
            elif isinstance(outcome, NoPlanFoundWithinSearchLimits):
                preservation = (
                    "your existing plan was kept"
                    if state.saved_plan is not None
                    else "no plan was saved"
                )
                status_text.value = (
                    "No complete plan was found within the current search limits. "
                    f"Edit the settings or cancel; {preservation}."
                )
            elif isinstance(outcome, NoFeasiblePlanProven):
                preservation = (
                    "Your existing plan was kept."
                    if state.saved_plan is not None
                    else "No plan was saved."
                )
                status_text.value = (
                    f"No feasible plan was proven for these settings. {preservation}"
                )
        except Exception as exc:  # noqa: BLE001 - surface errors in the UI
            status_text.value = f"Something went wrong: {type(exc).__name__}: {exc}"
        finally:
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
            [
                budget_field,
                mode_button,
                zip_field,
                range_button,
                plan_button,
                *([delete_plan_button] if delete_plan_button is not None else []),
            ],
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
        muted_text(ESTIMATED_CAP_DISCLAIMER, size=12),
        *replaces_note,
        progress_bar,
        status_text,
        subtitle=(
            "Set an estimated cap, dates, and a variety style. RightMeal builds "
            "real recipes around the current basket estimate."
        ),
    )

    return ft.Column(
        [inputs_card],
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
