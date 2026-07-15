"""Plan page: renders the saved plan — basket, daily meals, and nutrition."""

from __future__ import annotations

from datetime import date
from typing import Callable

import flet as ft

import theme
from models import (
    PRICE_SOURCE_LABELS,
    Explanation,
    Food,
    FoodGroup,
    Nutrients,
    PriceSource,
    SavedPlan,
)
from models.purchase_log import (
    ORIGIN_DIRECT_BUTTON,
    PurchaseInput,
    new_purchase_event_id,
)
from planner import pantry_used_totals, prepared_meal_totals
from services.pantry_flow import (
    can_void_group,
    latest_group_for_food,
    record_purchase_events,
    void_purchase_group,
)
from services.source_allocation import (
    GRAM_EPSILON,
    FoodAllocation,
    allocate_sources,
    covered_nutrients,
    dynamic_open_cost,
    is_historical,
)
from ui.components import (
    GapRow,
    basket_card,
    budget_bar,
    card,
    collapsible_section,
    food_avatar,
    muted_text,
    nutrition_card,
    pill,
    primary_button,
    source_mix_caption,
    warnings_strip,
)
from ui.meals_section import carryover_amount_label, daily_meals_section
from ui.photo_purchase import ensure_file_picker, run_receipt_flow
from ui.state import AppState


def build_gap_rows(consumed: Nutrients, available: Nutrients, targets: Nutrients) -> list[GapRow]:
    """``available`` is the honest three-source sum (purchases + pantry grams
    the plan uses + scheduled prepared leftovers) — no clamping that would
    paper over an accounting mismatch."""
    return [
        GapRow(
            name,
            consumed.get(name),
            available.get(name),
            targets.get(name),
        )
        for name in Nutrients.NAMES
    ]


def pantry_coverage_note(food: Food, from_pantry: float) -> str | None:
    if from_pantry <= GRAM_EPSILON:
        return None
    return f"{carryover_amount_label(food, from_pantry)} covered by pantry"


def _empty_state(on_go_to_start: Callable[[], None]) -> ft.Container:
    button = primary_button("Go to Start", icon=ft.Icons.ROCKET_LAUNCH_OUTLINED)
    button.on_click = lambda e: on_go_to_start()
    return card(
        ft.Column(
            [
                ft.Icon(ft.Icons.SHOPPING_BASKET_OUTLINED, size=48, color=theme.TEXT_MUTED),
                ft.Text("No plan yet", size=16, weight=ft.FontWeight.W_600, color=theme.TEXT),
                muted_text(
                    "Set a budget and dates on the Start tab and RightMeal builds "
                    "your basket and daily meals."
                ),
                ft.Container(height=4),
                button,
            ],
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=40,
    )


def _pantry_use_block(
    state: AppState,
    entries: list[tuple[str, float]],
    title: str,
    subtitle: str,
) -> ft.Column:
    """Rows for pantry grams sourcing this plan — live allocation on the
    active plan, the frozen snapshot on a historical one."""
    foods_by_id = state.foods_by_id
    rows: list[ft.Control] = [
        ft.Text(title, size=13.5, weight=ft.FontWeight.W_600, color=theme.TEXT),
        muted_text(subtitle, size=12.5),
    ]
    for food_id, grams in entries:
        food = foods_by_id.get(food_id)
        if food is None:
            continue
        rows.append(
            ft.Row(
                [
                    food_avatar(food, size=24, image_src=state.image_src_for(food)),
                    ft.Text(food.name, size=12.5, color=theme.TEXT, expand=True,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    muted_text(f"Use {carryover_amount_label(food, grams)} from pantry", size=12),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
    return ft.Column(rows, spacing=8)


def _basket_section(page: ft.Page, plan: SavedPlan, state: AppState) -> ft.Container:
    foods_by_id = state.foods_by_id
    live = not is_historical(plan)

    # Original basket lines grouped by food — the frozen snapshot. It drives
    # price-match details, historical rendering, and first-seen card order;
    # the LIVE to-buy quantities come from allocate_sources instead.
    grouped: dict[str, list] = {}
    for item in plan.basket:
        if item.food_id in foods_by_id:
            grouped.setdefault(item.food_id, []).append(item)

    body_column = ft.Column(spacing=12)
    ui_state = {"purchased_expanded": False}
    receipt_picker = ensure_file_picker(page)

    def on_receipt_committed() -> None:
        rebuild()
        page.update()

    async def on_receipt_click(e) -> None:
        await run_receipt_flow(
            page, state, receipt_picker, on_receipt_committed
        )

    receipt_button = ft.FilledTonalButton(
        content="Import receipt photos",
        icon=ft.Icons.RECEIPT_LONG_OUTLINED,
        on_click=on_receipt_click,
        tooltip="Review visible receipt lines before adding them to Pantry or Plan",
    )

    def commit(mutate: Callable[[], None]) -> bool:
        """Snapshot → mutate → persist all touched stores; roll back memory on
        failure — the disk transaction is all-or-nothing, so memory must be too."""
        snapshot = (dict(state.pantry.items), dict(plan.purchased),
                    dict(plan.purchased_baseline), list(state.purchase_log))
        mutate()
        try:
            state.persist(plan=plan, pantry=state.pantry, purchases=state.purchase_log)
            return True
        except Exception:  # noqa: BLE001 - restore and surface
            state.pantry.items.clear()
            state.pantry.items.update(snapshot[0])
            plan.purchased.clear()
            plan.purchased.update(snapshot[1])
            plan.purchased_baseline.clear()
            plan.purchased_baseline.update(snapshot[2])
            state.purchase_log[:] = snapshot[3]
            page.show_dialog(ft.SnackBar(ft.Text("Couldn't save — nothing was changed.")))
            return False

    def purchases_paused() -> bool:
        if state.purchase_log_error:
            page.show_dialog(ft.SnackBar(ft.Text(
                "Purchase history could not be read — purchasing is paused to "
                "protect your data."
            )))
            return True
        return False

    def purchase_handler(food, allocation: FoodAllocation) -> Callable[[bool], None]:
        """One button press = one event GROUP: a PurchaseRecord per BuyLine of
        the LIVE allocation — pantry changes since plan time shrink or grow
        what a click buys, never the frozen basket."""
        def handler(checked: bool) -> None:
            if not checked or purchases_paused():
                return  # cards in "Need to buy" only ever check on
            group_id = new_purchase_event_id()
            inputs = [
                PurchaseInput(
                    event_id=new_purchase_event_id(),
                    food_id=food.id,
                    raw_name=food.name,
                    package_label=line.package_label,
                    grams=line.package_grams * line.count,
                    quantity=line.count,
                    estimated_line_cost=line.est_cost,
                    apply_to_plan=True,
                    group_id=group_id,
                    origin=ORIGIN_DIRECT_BUTTON,
                )
                for line in allocation.to_buy
            ]
            if not inputs:
                return
            if commit(lambda: record_purchase_events(
                plan, state.pantry, state.purchase_log, inputs
            )):
                rebuild()
                page.update()

        return handler

    def undo_handler(group_id: str) -> Callable:
        def handler(e) -> None:
            if purchases_paused():
                return
            ok, message = can_void_group(plan, state.pantry, state.purchase_log, group_id)
            if not ok:
                if message:
                    page.show_dialog(ft.SnackBar(ft.Text(message)))
                rebuild()
                page.update()
                return
            commit(lambda: void_purchase_group(
                plan, state.pantry, state.purchase_log, group_id
            ))
            rebuild()
            page.update()

        return handler

    def price_details(items: list) -> list[tuple[str, str]]:
        """Price-match details from the original basket lines for this food.

        All packages of one food share a quote today (optimizer keys quotes by
        food.id), so their details are identical. Guard that: show the shared
        details, or a neutral line if a future source diverges."""
        if not items:
            return [("Price source", "Seed estimate — this need appeared after "
                                     "the plan was built")]
        first = items[0]
        uniform = all(
            (it.source, it.store, it.matched_product_name, it.confidence, it.match_reason)
            == (first.source, first.store, first.matched_product_name, first.confidence,
                first.match_reason)
            for it in items
        )
        if uniform:
            return [
                ("Matched product", first.matched_product_name),
                ("Store", first.store),
                ("Price source", PRICE_SOURCE_LABELS[PriceSource(first.source)]),
                ("Match confidence", f"{first.confidence:.0%}"),
                ("Why this match", first.match_reason),
            ]
        return [("Price source", "Mixed package sources")]

    def live_card(food_id: str, allocation: FoodAllocation) -> ft.Control:
        food = foods_by_id[food_id]
        ordered = sorted(allocation.to_buy, key=lambda line: line.package_grams, reverse=True)
        quantity_label = " + ".join(
            line.package_label if line.count == 1 else f"{line.count} × {line.package_label}"
            for line in ordered
        )
        total_cost = sum(line.est_cost for line in allocation.to_buy)
        return basket_card(
            food,
            quantity_label,
            total_cost,
            price_details(grouped.get(food_id, [])),
            image_src=state.image_src_for(food),
            purchased=False,  # purchased items move to the Purchased section below
            on_purchased_change=purchase_handler(food, allocation),
            pantry_note=pantry_coverage_note(food, allocation.from_pantry),
            pantry_note_tooltip="Also counted in 'Available for remaining meals' above",
        )

    def snapshot_card(food_id: str, items: list) -> ft.Control:
        food = foods_by_id[food_id]
        grams_by_label = {pkg.label: pkg.grams for pkg in food.package_options}
        # Sort fragments by real package weight (largest first), not label text.
        ordered = sorted(
            items, key=lambda it: grams_by_label.get(it.package_label, 0.0), reverse=True
        )
        quantity_label = " + ".join(
            it.package_label if it.count == 1 else f"{it.count} × {it.package_label}"
            for it in ordered
        )
        total_cost = sum(it.cost for it in items)
        return basket_card(
            food,
            quantity_label,
            total_cost,
            price_details(ordered),
            image_src=state.image_src_for(food),
            purchased=None,  # historical plans take no purchase actions
        )

    def purchased_row(food_id: str) -> ft.Row:
        food = foods_by_id[food_id]
        grams = plan.purchased.get(food_id, 0.0)
        # Undo targets the food's latest event GROUP (one button press / one
        # receipt) — voided whole or not at all.
        group_id = latest_group_for_food(state.purchase_log, food_id)
        undo_button = ft.TextButton(content="Undo")
        if not live:
            undo_button.disabled = True
            undo_button.tooltip = "This plan has ended — its purchase history is frozen."
        elif group_id is None:
            undo_button.disabled = True
            undo_button.tooltip = "No purchase event recorded for this food."
        else:
            undo_button.on_click = undo_handler(group_id)
            ok, reason = can_void_group(plan, state.pantry, state.purchase_log, group_id)
            if ok:
                undo_button.tooltip = (
                    "Undo this purchase — removes exactly what it added to the pantry"
                )
            else:
                undo_button.disabled = True
                undo_button.tooltip = reason
        return ft.Row(
            [
                food_avatar(food, size=28, image_src=state.image_src_for(food)),
                ft.Text(food.name, size=12.5, color=theme.TEXT, expand=True,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                muted_text(f"+{carryover_amount_label(food, grams)} to pantry", size=12),
                undo_button,
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def on_purchased_toggle(e) -> None:
        ui_state["purchased_expanded"] = e.data == "true"

    def rebuild() -> None:
        body: list[ft.Control] = []
        if live:
            body.append(ft.Row([receipt_button]))
        done = [fid for fid in plan.purchased if fid in foods_by_id]
        if live:
            allocations = allocate_sources(plan, state.pantry, foods_by_id)
            available = [
                (fid, alloc.from_pantry)
                for fid, alloc in sorted(allocations.items())
                if alloc.from_pantry > GRAM_EPSILON
            ]
            if available:
                body.append(_pantry_use_block(
                    state, available,
                    "Available for remaining meals",
                    "Already at home — reserved for this plan's remaining meals.",
                ))
                body.append(ft.Divider(height=1, color=theme.BORDER))
            # First-seen basket order, then any needs that appeared later.
            to_buy = [fid for fid in grouped if allocations.get(fid) and allocations[fid].to_buy]
            to_buy += [
                fid for fid in sorted(allocations)
                if fid not in grouped and allocations[fid].to_buy
            ]
            if to_buy or available or done:
                body.append(ft.Text(
                    "Need to buy", size=13.5, weight=ft.FontWeight.W_600, color=theme.TEXT
                ))
            if to_buy:
                body.append(ft.Row(
                    [live_card(fid, allocations[fid]) for fid in to_buy],
                    wrap=True, spacing=12, run_spacing=12,
                ))
            elif done:
                body.append(muted_text(
                    "Everything's checked off — it's all in your pantry.", size=12.5
                ))
            elif available:
                body.append(muted_text(
                    "Nothing to buy — your pantry covers this plan.", size=12.5
                ))
        else:
            if plan.pantry_used:
                body.append(_pantry_use_block(
                    state, sorted(plan.pantry_used.items()),
                    "Used from pantry",
                    "What this plan drew from stock you already had.",
                ))
                body.append(ft.Divider(height=1, color=theme.BORDER))
            to_buy = [fid for fid in grouped if fid not in plan.purchased]
            if grouped or plan.pantry_used:
                body.append(ft.Text(
                    "Shopping list (plan ended)", size=13.5,
                    weight=ft.FontWeight.W_600, color=theme.TEXT,
                ))
            if to_buy:
                body.append(ft.Row(
                    [snapshot_card(fid, grouped[fid]) for fid in to_buy],
                    wrap=True, spacing=12, run_spacing=12,
                ))
        if done:
            body.append(ft.ExpansionTile(
                title=ft.Row(
                    [
                        ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=16, color=theme.PRIMARY),
                        ft.Text(f"Purchased ({len(done)})", size=13.5,
                                weight=ft.FontWeight.W_600, color=theme.TEXT),
                    ],
                    spacing=8,
                ),
                expanded=ui_state["purchased_expanded"],
                on_change=on_purchased_toggle,
                shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                collapsed_shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                icon_color=theme.TEXT_MUTED,
                collapsed_icon_color=theme.TEXT_MUTED,
                tile_padding=ft.Padding.symmetric(horizontal=4),
                controls_padding=ft.Padding.only(left=4, right=4, bottom=8),
                controls=[ft.Column([purchased_row(fid) for fid in done], spacing=8)],
            ))
        if plan.staples:
            body.append(ft.Divider(height=1, color=theme.BORDER))
            body.append(ft.Text(
                "Pantry staples to check", size=13.5, weight=ft.FontWeight.W_600,
                color=theme.TEXT))
            body.append(muted_text(
                "Common seasonings the recipes use — check you have them (not priced): "
                + ", ".join(plan.staples) + ".", size=12.5))
        body_column.controls = body

    rebuild()

    return collapsible_section(
        "Your basket",
        body_column,
        icon=ft.Icons.SHOPPING_BASKET_OUTLINED,
        icon_tooltip="The full supermarket shopping list for this plan",
        source_note=(
            "Prices are planning estimates mixing live Kroger pricing, USDA BLS "
            "regional averages, and seed estimates — see each item's own ⓘ for "
            "its exact source."
        ),
        subtitle=f"Shopping and pantry sourcing for the {plan.horizon_days}-day plan",
        expanded=True,
    )


def _explanation_section(explanation: Explanation) -> ft.Container:
    children: list[ft.Control] = [ft.Text(explanation.summary, size=13.5, color=theme.TEXT)]
    if explanation.item_reasons:
        children.append(
            ft.ExpansionTile(
                title=ft.Text("Item by item", size=13.5, weight=ft.FontWeight.W_500, color=theme.TEXT),
                shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                collapsed_shape=ft.RoundedRectangleBorder(radius=theme.RADIUS_SM),
                icon_color=theme.TEXT_MUTED,
                collapsed_icon_color=theme.TEXT_MUTED,
                tile_padding=ft.Padding.symmetric(horizontal=4),
                controls=[
                    ft.Container(
                        content=ft.Text(f"{name}: {reason}", size=13, color=theme.TEXT),
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
            children.append(ft.Text(text, size=13, color=theme.TEXT))
    if explanation.generated_by == "openai":
        tag = pill("AI-assisted", theme.PRIMARY_TINT, theme.PRIMARY_DARK)
    else:
        tag = pill("Generated locally", theme.SURFACE_TINT, theme.TEXT_MUTED)
    children.append(ft.Row([tag]))
    return collapsible_section(
        "Why this basket?",
        *children,
        icon=ft.Icons.LIGHTBULB_OUTLINE,
        icon_tooltip="How the budget and nutrition targets shaped these picks",
        expanded=False,
    )


def _plan_sidebar(
    nav_items: list[tuple[str, str, str]], content_column: ft.Column
) -> ft.Container:
    """A translucent, icon-only floating rail that jumps the content column
    to a section. Tooltips carry the labels since there's no room for text."""

    def nav_button(icon: str, label: str, scroll_key: str) -> ft.Container:
        async def go(e):
            await content_column.scroll_to(scroll_key=scroll_key, duration=300)

        return ft.Container(
            content=ft.Icon(icon, size=18, color=theme.TEXT_MUTED),
            width=40,
            height=40,
            alignment=ft.Alignment.CENTER,
            border_radius=999,
            ink=True,
            tooltip=label,
            on_click=go,
        )

    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=6, vertical=10),
        border_radius=999,
        bgcolor=ft.Colors.with_opacity(0.6, theme.SURFACE),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.5, theme.BORDER)),
        shadow=ft.BoxShadow(
            spread_radius=0,
            blur_radius=20,
            color=ft.Colors.with_opacity(0.10, "#12301C"),
            offset=ft.Offset(0, 6),
        ),
        content=ft.Column(
            [nav_button(icon, label, key) for icon, label, key in nav_items],
            spacing=4,
            tight=True,
        ),
    )


def build_planning_view(
    page: ft.Page,
    state: AppState,
    on_go_to_start: Callable[[], None],
    initial_date: date | None = None,
) -> ft.Control:
    plan = state.saved_plan
    if plan is None:
        return _empty_state(on_go_to_start)

    profile = state.profile
    foods_by_id = state.foods_by_id
    # Live plans re-derive ingredient sourcing from the current pantry;
    # historical plans render exclusively from their frozen snapshot.
    allocations = (
        allocate_sources(plan, state.pantry, foods_by_id)
        if not is_historical(plan)
        else None
    )
    controls: list[ft.Control] = []
    nav_items: list[tuple[str, str, str]] = []

    strip = warnings_strip(
        plan.nutrition_feasible,
        plan.budget_status,
        plan.relaxed_constraints,
        plan.dominance_flags,
    )
    if strip is not None:
        controls.append(strip)

    if plan.basket or plan.pantry_used:
        if plan.basket:
            # One entry per distinct food (each food has one quote/source today), so
            # multiple package sizes of a food don't inflate the provenance caption.
            source_mix: dict[PriceSource, int] = {}
            for food_id in {item.food_id for item in plan.basket}:
                sources = [
                    PriceSource(item.source) for item in plan.basket if item.food_id == food_id
                ]
                source_mix[sources[0]] = source_mix.get(sources[0], 0) + 1
            if allocations is not None:
                est_purchased = sum(
                    item.cost for item in plan.basket if item.food_id in plan.purchased
                )
                still_needed = dynamic_open_cost(allocations)
                controls.append(
                    budget_bar(
                        est_purchased + still_needed, plan.budget, plan.horizon_days,
                        source_mix_caption(source_mix),
                        status_line=(
                            f"Estimated purchased: ${est_purchased:.2f} · "
                            f"Estimated still needed: ${still_needed:.2f} · "
                            f"Original estimate: ${plan.total_cost:.2f}"
                        ),
                    )
                )
            else:
                controls.append(
                    budget_bar(
                        plan.total_cost, plan.budget, plan.horizon_days,
                        source_mix_caption(source_mix),
                    )
                )

        basket_section = _basket_section(page, plan, state)
        basket_section.key = ft.ScrollKey("section-basket")
        controls.append(basket_section)
        nav_items.append((ft.Icons.SHOPPING_BASKET_OUTLINED, "Basket", "section-basket"))

        meals_section = daily_meals_section(
            page,
            state,
            plan,
            state.nutrition.household_daily_targets(profile),
            initial_date=initial_date,
        )
        meals_section.key = ft.ScrollKey("section-meals")
        controls.append(meals_section)
        nav_items.append((ft.Icons.RESTAURANT_MENU, "Daily meals", "section-meals"))

        covered_groups: set[FoodGroup] = {
            foods_by_id[food_id].food_group
            for food_id in (
                {item.food_id for item in plan.basket} | set(plan.pantry_used)
            )
            if food_id in foods_by_id
        }
        targets = state.nutrition.household_targets(profile, plan.horizon_days)
        if allocations is not None:
            # Covered = what current stock + completed cooking secure of the
            # plan's meals (live allocation), plus scheduled ready-meals —
            # they draw zero raw ingredients, so there is no overlap. Batch
            # leftovers are already inside the allocation (2× dinner draw).
            available = covered_nutrients(allocations, foods_by_id).plus(
                prepared_meal_totals(plan.meal_plan)
            )
        else:
            # Historical snapshot: the frozen three-source sum.
            available = plan.purchased_totals.plus(
                pantry_used_totals(plan.pantry_used, foods_by_id)
            ).plus(prepared_meal_totals(plan.meal_plan))
        gap_rows = build_gap_rows(plan.meal_plan.consumed_totals, available, targets)
        nutrition_section = nutrition_card(gap_rows, covered_groups, plan.horizon_days)
        nutrition_section.key = ft.ScrollKey("section-nutrition")
        controls.append(nutrition_section)
        nav_items.append((ft.Icons.MONITOR_HEART, "Nutrition", "section-nutrition"))

    if plan.explanation is not None:
        why_section = _explanation_section(plan.explanation)
        why_section.key = ft.ScrollKey("section-why")
        controls.append(why_section)
        nav_items.append((ft.Icons.LIGHTBULB_OUTLINE, "Why this basket?", "section-why"))

    content_column = ft.Column(
        controls,
        spacing=16,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
    if not nav_items:
        return content_column
    return ft.Stack(
        [
            ft.Container(
                content=content_column,
                padding=ft.Padding.only(left=56),
                left=0,
                top=0,
                right=0,
                bottom=0,
            ),
            ft.Container(content=_plan_sidebar(nav_items, content_column), left=4, top=4),
        ],
        expand=True,
    )
