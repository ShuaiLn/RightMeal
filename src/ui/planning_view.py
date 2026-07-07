"""Main planning page: budget/location/horizon inputs and basket results."""

from __future__ import annotations

import flet as ft

import theme
from data import load_bls_price_map
from models import Explanation, HouseholdProfile, Location, OptimizationResult
from optimizer import optimize
from services.explanation import get_explanation_service
from services.keys import resolve_key
from services.price_engine import PriceEngine
from services.price_providers import (
    BlsProvider,
    InstacartProvider,
    KrogerProvider,
    SeedProvider,
)
from ui.components import (
    budget_bar,
    gaps_card,
    group_coverage_row,
    highlight_card,
    muted_text,
    section_card,
    source_chip,
    warnings_strip,
)
from ui.household_form import ZIP_RE
from ui.state import AppState


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


def build_planning_view(page: ft.Page, state: AppState) -> ft.Control:
    profile: HouseholdProfile = state.profile

    budget_field = ft.TextField(
        label="Budget (USD)", value="50", width=150, keyboard_type=ft.KeyboardType.NUMBER
    )
    mode_button = ft.SegmentedButton(
        segments=[
            ft.Segment(value="weekly", label="Weekly"),
            ft.Segment(value="daily", label="Daily"),
        ],
        selected=["weekly"],
    )
    zip_field = ft.TextField(label="ZIP code", value=profile.zip_code, width=130)
    horizon_dropdown = ft.Dropdown(
        label="Plan for",
        value="7",
        width=150,
        options=[ft.DropdownOption(key=str(d), text=f"{d} day{'s' if d > 1 else ''}") for d in range(1, 15)],
    )
    caption = muted_text("", size=13)
    plan_button = ft.FilledButton(content="Plan my groceries", icon=ft.Icons.SHOPPING_BASKET)

    progress_bar = ft.ProgressBar(value=0, color=theme.DEEP_GREEN, bgcolor=theme.BG_WHITE, visible=False)
    status_text = muted_text("", size=13)
    results_column = ft.Column(spacing=14, visible=False)

    def parse_inputs() -> tuple[float, int, str] | None:
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
        horizon = int(horizon_dropdown.value or "7")
        return (amount, horizon, zip_field.value) if ok else None

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
        horizon = int(horizon_dropdown.value or "7")
        caption.value = f"= ${planning_budget(amount, horizon):.2f} for {horizon} days"
        page.update()

    budget_field.on_change = update_caption
    mode_button.on_change = update_caption
    horizon_dropdown.on_change = update_caption

    async def on_plan_click(e):
        parsed = parse_inputs()
        page.update()
        if parsed is None:
            return
        amount, horizon, zip_code = parsed
        budget = round(planning_budget(amount, horizon), 2)
        location = Location(city=profile.city, zip_code=zip_code)

        plan_button.disabled = True
        progress_bar.visible = True
        progress_bar.value = 0
        results_column.visible = False
        status_text.value = "Looking up prices…"
        page.update()

        def on_progress(done: int, total: int):
            progress_bar.value = done / total
            status_text.value = f"Pricing {done} of {total} foods…"
            page.update()

        try:
            engine = build_engine(state)
            quotes = await engine.price_all(list(state.foods), location, on_progress=on_progress)

            status_text.value = "Optimizing your basket…"
            page.update()
            result = optimize(
                foods=state.foods,
                quotes=quotes,
                profile=profile,
                budget=budget,
                horizon_days=horizon,
                nutrition=state.nutrition,
            )

            status_text.value = "Writing the explanation…"
            page.update()
            service = get_explanation_service(profile, state.http_client)
            explanation = await service.explain(result, profile)

            render_results(result, explanation)
            status_text.value = ""
        except Exception as exc:  # noqa: BLE001 - surface errors in the UI
            status_text.value = f"Something went wrong: {type(exc).__name__}: {exc}"
        finally:
            plan_button.disabled = False
            progress_bar.visible = False
            page.update()

    plan_button.on_click = on_plan_click

    def basket_table(result: OptimizationResult) -> ft.Control:
        rows = []
        for item in result.items:
            name = ft.Text(
                item.food.name,
                overflow=ft.TextOverflow.ELLIPSIS,
                max_lines=1,
                color=theme.TEXT_DARK,
                tooltip=f"{item.quote.matched_product_name} — {item.quote.store}",
            )
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(content=name),
                        ft.DataCell(content=ft.Text(item.quantity_label, color=theme.TEXT_DARK)),
                        ft.DataCell(content=ft.Text(f"${item.cost:.2f}", color=theme.TEXT_DARK)),
                        ft.DataCell(content=source_chip(item.quote)),
                        ft.DataCell(
                            content=ft.Text(f"{item.quote.confidence:.0%}", color=theme.TEXT_DARK)
                        ),
                    ]
                )
            )
        table = ft.DataTable(
            columns=[
                ft.DataColumn(label=ft.Text("Item", weight=ft.FontWeight.BOLD)),
                ft.DataColumn(label=ft.Text("Quantity", weight=ft.FontWeight.BOLD)),
                ft.DataColumn(label=ft.Text("Est. price", weight=ft.FontWeight.BOLD)),
                ft.DataColumn(label=ft.Text("Price source", weight=ft.FontWeight.BOLD)),
                ft.DataColumn(label=ft.Text("Match", weight=ft.FontWeight.BOLD)),
            ],
            rows=rows,
            column_spacing=18,
        )
        # Horizontal scroll keeps narrow windows overflow-free.
        return ft.Row([table], scroll=ft.ScrollMode.AUTO)

    def explanation_card(result: OptimizationResult, explanation: Explanation) -> ft.Container:
        children: list[ft.Control] = [ft.Text(explanation.summary, color=theme.TEXT_DARK)]
        if explanation.item_reasons:
            children.append(
                ft.ExpansionTile(
                    title=ft.Text("Item by item", size=14, color=theme.TEXT_DARK),
                    controls=[
                        ft.Container(
                            content=ft.Text(f"{name}: {reason}", size=13, color=theme.TEXT_DARK),
                            padding=ft.Padding.symmetric(horizontal=16, vertical=4),
                        )
                        for name, reason in explanation.item_reasons.items()
                    ],
                )
            )
        for text in (
            explanation.budget_tradeoffs,
            explanation.food_group_coverage,
            explanation.life_impact,
        ):
            if text:
                children.append(ft.Text(text, size=13, color=theme.TEXT_DARK))
        tag = "AI-assisted" if explanation.generated_by == "openai" else "Generated locally"
        children.append(muted_text(tag))
        return section_card("Why this basket?", *children)

    def render_results(result: OptimizationResult, explanation: Explanation) -> None:
        controls: list[ft.Control] = []
        strip = warnings_strip(result)
        if strip is not None:
            controls.append(strip)
        if result.items:
            controls.append(budget_bar(result))
            controls.append(section_card("Your basket", basket_table(result)))
            controls.append(section_card(None, group_coverage_row(result)))
            controls.append(gaps_card(result))
        controls.append(explanation_card(result, explanation))
        if result.excluded_foods:
            foods_by_id = {f.id: f for f in state.foods}
            lines = [
                f"{foods_by_id[food_id].name} — {reason}"
                for food_id, reason in sorted(result.excluded_foods.items())
            ]
            controls.append(
                section_card("Excluded from planning", *[muted_text(line, size=13) for line in lines])
            )
        results_column.controls = controls
        results_column.visible = True

    update_caption()

    inputs_card = highlight_card(
        ft.Text("Plan your groceries", size=18, weight=ft.FontWeight.BOLD, color=theme.TEXT_DARK),
        ft.Row(
            [budget_field, mode_button, zip_field, horizon_dropdown, plan_button],
            wrap=True,
            spacing=10,
            run_spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        caption,
        progress_bar,
        status_text,
    )

    return ft.Column(
        [inputs_card, results_column],
        spacing=14,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
