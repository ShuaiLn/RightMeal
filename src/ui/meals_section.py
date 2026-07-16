"""Daily meals section: a plan-days calendar strip, big-photo meal cards for the
selected day (with the eaten/leftover tracking footer), a per-day nutrition
panel, and the pantry carryover."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Callable

import flet as ft

import theme
from models import (
    SLOT_LABELS,
    SLOT_ORDER,
    Food,
    HouseholdProfile,
    Meal,
    MealPlan,
    MealPortion,
    MealSlot,
    Nutrients,
    PlanKind,
    SavedPlan,
)
from planner import SHORT_NAMES
from services.leftover_analyzer import get_leftover_analyzer
from services.meal_presentation import representative_food_for_meal
from services.recipe_service import (
    build_recipe_request,
    get_recipe_service,
    recipe_cache_key,
)
from services.meal_tracking_flow import (
    TrackingResult,
    can_edit_leftover,
    can_undo_preparation,
    correct_display_status,
    correct_leftover_note,
    meal_was_prepared,
    prepare_and_eat,
    record_leftover,
    undo_preparation,
)
from ui.components import (
    NUTRIENT_STYLES,
    collapsible_section,
    food_avatar,
    muted_text,
    nutrient_fraction,
    pill,
    style_field,
)

if TYPE_CHECKING:
    from ui.state import AppState

SLOT_ICONS: dict[MealSlot, str] = {
    MealSlot.BREAKFAST: ft.Icons.FREE_BREAKFAST,
    MealSlot.LUNCH: ft.Icons.LUNCH_DINING,
    MealSlot.DINNER: ft.Icons.DINNER_DINING,
}

# A fixed height is required for the meal-card row: CrossAxisAlignment.STRETCH
# needs a bounded cross-axis constraint, but the row sits inside a Column with
# scroll=AUTO (unbounded height) — without it, Flutter can't resolve the
# stretch and silently fails to render the row (and everything after it in the
# scroll view). Sized for the tallest footer state; adjust here, never remove.
MEAL_CARD_ROW_HEIGHT = 450

ImageSrcFor = Callable[[Food], "bytes | str | None"]

PARTIAL_FOOD_COVERAGE_WARNING = (
    "This is not a complete food plan. Additional food is required."
)
STALE_PLAN_WARNING = (
    "This plan was generated for an earlier household profile. "
    "Build a new plan to update portions and dietary restrictions."
)


def plan_warning_banner(message: str, *, stale: bool = False, compact: bool = False) -> ft.Container:
    """An always-visible plan warning used by Plan, Calendar, and meal details."""

    ink = theme.DANGER if stale else theme.WARN_INK
    return ft.Container(
        content=ft.Row(
            [
                ft.Icon(
                    ft.Icons.PERSON_OFF_OUTLINED if stale else ft.Icons.WARNING_AMBER_ROUNDED,
                    size=16 if compact else 18,
                    color=ink,
                ),
                ft.Text(
                    message,
                    size=11.5 if compact else 12.5,
                    weight=ft.FontWeight.W_600,
                    color=ink,
                    expand=True,
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=theme.DANGER_TINT if stale else theme.WARN_BG,
        border=ft.Border.all(1, theme.DANGER if stale else theme.WARN_BORDER),
        border_radius=theme.RADIUS_SM,
        padding=8 if compact else 12,
    )


def partial_plan_warning_banner(*, compact: bool = False) -> ft.Container:
    return plan_warning_banner(PARTIAL_FOOD_COVERAGE_WARNING, compact=compact)


def stale_plan_warning_banner(*, compact: bool = False) -> ft.Container:
    return plan_warning_banner(STALE_PLAN_WARNING, stale=True, compact=compact)


def _meal_member_count(meal: Meal) -> int:
    if meal.household_member_count > 0:
        return meal.household_member_count
    if meal.servings > 0:
        return max(1, int(round(meal.servings)))
    return 0


def meal_serving_summary(meal: Meal) -> str:
    """Make reduced portions explicit without calling them fewer people."""

    members = _meal_member_count(meal)
    people = (
        f"{members} {'person' if members == 1 else 'people'}"
        if members
        else "not recorded"
    )
    equivalents = meal.full_serving_equivalent
    if equivalents <= 0:
        equivalents = meal.servings
    equivalent_label = (
        "full-serving equivalent" if abs(equivalents - 1.0) < 1e-9
        else "full-serving equivalents"
    )
    return (
        f"Household: {people} · {equivalents:g} {equivalent_label} · "
        f"{meal.portion_scale * 100:.0f}% portions"
    )


def meal_per_person_nutrition_label(meal: Meal) -> str:
    """Estimated calories and protein divided across the actual household."""

    members = _meal_member_count(meal)
    if members <= 0:
        return f"Estimated nutrition: ≈{meal.kcal:,.0f} kcal total"
    nutrients = meal.nutrients
    return (
        f"Per person (estimated): ≈{nutrients.calories_kcal / members:,.0f} kcal · "
        f"≈{nutrients.protein_g / members:,.1f} g protein"
    )


def _fmt_date(when: date) -> str:
    return f"{when.strftime('%b')} {when.day}"


def _fmt_grams(grams: float) -> str:
    if grams >= 1000:
        return f"{grams / 1000:.1f} kg"
    return f"{grams:.0f} g"


def portion_label(portion: MealPortion) -> str:
    """e.g. 'oats 80 g dry (≈ 200 g cooked)', 'milk 250 ml', 'banana 120 g'."""
    food = portion.food
    name = SHORT_NAMES.get(food.id, food.name.lower())
    if food.is_liquid and food.density_g_per_ml:
        ml = portion.grams / food.density_g_per_ml
        return f"{name} {ml:.0f} ml"
    if portion.cooked_grams is not None:
        return f"{name} {portion.grams:.0f} g dry (≈ {portion.cooked_grams:.0f} g cooked)"
    return f"{name} {_fmt_grams(portion.grams)}"


def carryover_amount_label(food: Food, grams: float) -> str:
    if food.is_liquid and food.density_g_per_ml:
        ml = grams / food.density_g_per_ml
        return f"{ml / 1000:.1f} L" if ml >= 1000 else f"{ml:.0f} ml"
    label = _fmt_grams(grams)
    if food.form == "dry":
        label += " dry"
    return label


def _slot_pill(slot: MealSlot, active: bool = True) -> ft.Container:
    ink = theme.PRIMARY_DARK if active else theme.TEXT_MUTED
    return ft.Container(
        content=ft.Row(
            [
                ft.Icon(SLOT_ICONS[slot], size=14, color=ink),
                ft.Text(SLOT_LABELS[slot], size=11.5, weight=ft.FontWeight.W_600, color=ink),
            ],
            spacing=4,
        ),
        bgcolor=theme.PRIMARY_TINT if active else theme.SURFACE_TINT,
        border_radius=999,
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
    )


def _photo_container(
    src: "bytes | str", size: int, error_content: ft.Control, width: int | None = None
) -> ft.Container:
    return ft.Container(
        content=ft.Image(
            src=src,
            width=width,
            height=size,
            fit=ft.BoxFit.COVER,
            border_radius=theme.RADIUS_SM,
            error_content=error_content,
        ),
        width=width,
        height=size,
        border_radius=theme.RADIUS_SM,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
    )


def dish_or_ingredient_photo(
    meal: Meal, state: "AppState", size: int = 132, width: int | None = None
) -> ft.Control:
    """The meal's thumbnail: dish photo -> main-ingredient photo -> generic
    icon, each tier only invoked if the previous one actually fails to render
    (nested ``error_content``, not a single up-front branch) — so a recipe
    with ``image_asset`` set but a missing/corrupt asset file still falls
    back to a real ingredient photo instead of jumping straight to the icon.

    ``width`` is unset (fills the available card width) by default, matching
    the Plan tab's meal cards; pass it explicitly for a fixed-size square
    thumbnail like Calendar's inline 44×44 dish icon.
    """
    icon = _dish_placeholder(size, width=width)
    ingredient = representative_food_for_meal(meal)
    ingredient_src = state.image_src_for(ingredient) if ingredient is not None else None
    ingredient_ctrl: ft.Control = (
        _photo_container(ingredient_src, size, error_content=icon, width=width)
        if ingredient_src
        else icon
    )
    recipe = state.recipes_by_id.get(meal.recipe_id) if meal.recipe_id else None
    if recipe is not None and recipe.image_asset:
        return _photo_container(
            f"/{recipe.image_asset}", size, error_content=ingredient_ctrl, width=width
        )
    return ingredient_ctrl


def _dish_placeholder(size: int = 132, width: int | None = None) -> ft.Container:
    return ft.Container(
        content=ft.Icon(ft.Icons.RESTAURANT_MENU, size=int(size * 0.3), color=theme.TEXT_MUTED),
        width=width,
        height=size,
        bgcolor=theme.SURFACE_TINT,
        border_radius=theme.RADIUS_SM,
        alignment=ft.Alignment.CENTER,
    )


def portion_row(p: MealPortion) -> ft.Row:
    grams = f"{p.grams:.0f} g" + (
        f" (≈{p.cooked_grams:.0f} g cooked)" if p.cooked_grams is not None else ""
    )
    return ft.Row(
        [
            ft.Text(
                p.food.name,
                size=12.5,
                color=theme.TEXT,
                expand=True,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
            muted_text(grams, size=12),
            ft.Text(f"{p.kcal:,.0f} kcal", size=12, weight=ft.FontWeight.W_600, color=theme.TEXT),
        ],
        spacing=10,
    )


def _show_toast(page: ft.Page, message: str) -> None:
    """A transient notification pinned to the bottom-right corner."""
    left = max((page.width or 800) - 396, 16)
    page.show_dialog(ft.SnackBar(
        ft.Text(message, size=12.5),
        behavior=ft.SnackBarBehavior.FLOATING,
        margin=ft.Margin.only(left=left, right=16, bottom=16),
    ))


def meal_card(
    meal: Meal,
    image_src_for: ImageSrcFor,
    *,
    page: ft.Page,
    state: "AppState",
    saved: SavedPlan,
    when: date,
    on_changed: Callable[[], None],
) -> ft.Container:
    """One meal as a card: photos on top, name and details underneath, and a
    minimal status footer (Eaten button / Eaten ✓) pinned to the bottom.

    Clicking the card opens the meal detail dialog — ingredients, cooking
    steps, and the full leftover-tracking controls all live there.
    """
    name_row: list[ft.Control] = [
        ft.Text(
            meal.name,
            size=14,
            weight=ft.FontWeight.W_600,
            color=theme.TEXT,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
    ]
    if meal.prepared_leftover_id is not None:
        name_row.append(pill("prepared leftovers", theme.SURFACE_TINT, theme.TEXT_MUTED,
                             tooltip="A dish you already cooked, scheduled as this meal"))
    elif meal.is_leftover:
        name_row.append(pill("leftovers", theme.SURFACE_TINT, theme.TEXT_MUTED,
                             tooltip="Batch-cooked the evening before"))

    def show_meal_details(e) -> None:
        open_meal_detail_dialog(page, state, saved, when, meal, on_changed)

    portions_text = " · ".join(portion_label(p) for p in meal.portions)
    partial_warning = (
        [partial_plan_warning_banner(compact=True)]
        if saved.plan_kind is PlanKind.PARTIAL_FOOD_COVERAGE
        else []
    )
    return ft.Container(
        expand=True,
        bgcolor=theme.SURFACE_TINT,
        border_radius=theme.RADIUS_SM,
        padding=12,
        ink=True,
        on_click=show_meal_details,
        content=ft.Column(
            [
                ft.Row(
                    [_slot_pill(meal.slot)],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                dish_or_ingredient_photo(meal, state),
                ft.Row(name_row, spacing=8, wrap=True),
                muted_text(meal_serving_summary(meal), size=11.5),
                muted_text(meal_per_person_nutrition_label(meal), size=11.5),
                ft.Text(
                    portions_text,
                    size=12,
                    color=theme.TEXT_MUTED,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=portions_text,
                ),
                *partial_warning,
                ft.Container(expand=True),  # pins the footer to the card bottom
                _card_status(page, state, saved, when, meal, on_changed),
            ],
            spacing=8,
        ),
    )


def _card_status(
    page: ft.Page,
    state: "AppState",
    saved: SavedPlan,
    when: date,
    meal: Meal,
    on_changed: Callable[[], None],
) -> ft.Control:
    """The card-face footer: just an Eaten button (or the Eaten ✓ pill).
    Everything else — undo, leftovers, notes — lives in the detail dialog."""
    entry = saved.tracking_entry(when, meal.slot)
    if bool(entry.get("eaten")):
        return ft.Row([pill("Eaten ✓", theme.PRIMARY_TINT, theme.PRIMARY_DARK)])

    def handle_eat(e) -> None:
        freshly_prepared = not meal_was_prepared(entry)
        result = prepare_and_eat(state, saved, when, meal.slot, meal)
        if result.message:
            page.show_dialog(ft.SnackBar(ft.Text(result.message)))
        if result.ok:
            on_changed()
            if freshly_prepared and not (meal.is_leftover or meal.prepared_leftover_id is not None):
                _open_leftover_prompt(page, state, saved, when, meal, on_changed)
        else:
            page.update()

    eat_button = ft.FilledTonalButton(content="Eaten", icon=ft.Icons.RESTAURANT, height=34)
    eat_button.on_click = handle_eat
    return ft.Row([eat_button])


def open_meal_detail_dialog(
    page: ft.Page,
    state: "AppState",
    saved: SavedPlan,
    when: date,
    meal: Meal,
    on_changed: Callable[[], None],
) -> None:
    """The meal detail dialog: photos, kcal, per-ingredient rows, cooking
    steps, and the full eaten/leftover tracking controls.

    Tracking actions rebuild the dialog body in place and refresh the meals
    section behind it via ``on_changed``.
    """
    ui_state: dict = {"mode": None}  # None | "edit_amount" | "edit_note"
    body = ft.Column(spacing=10, tight=True, scroll=ft.ScrollMode.AUTO)

    def rerender() -> None:
        # Pure UI mode flip (e.g. entering edit mode) — no state changed.
        rebuild()
        page.update()

    def changed() -> None:
        on_changed()  # refresh the meal cards behind the dialog
        rebuild()
        page.update()

    def rebuild() -> None:
        recipe = state.recipes_by_id.get(meal.recipe_id) if meal.recipe_id else None
        body.controls = [
            *(
                [partial_plan_warning_banner()]
                if saved.plan_kind is PlanKind.PARTIAL_FOOD_COVERAGE
                else []
            ),
            *(
                [stale_plan_warning_banner()]
                if state.profile is not None and saved.profile_stale(state.profile)
                else []
            ),
            dish_or_ingredient_photo(meal, state),
            muted_text(meal_serving_summary(meal), size=12.5),
            muted_text(meal_per_person_nutrition_label(meal), size=12.5),
            *([muted_text(f"Source: {recipe.source_file}", size=11)] if recipe else []),
            ft.Divider(height=1, color=theme.BORDER),
            *[portion_row(p) for p in meal.portions],
            ft.Divider(height=1, color=theme.BORDER),
            _directions_block(page, state, meal, recipe, ui_state, rerender),
            ft.Divider(height=1, color=theme.BORDER),
            _tracking_block(page, state, saved, when, meal, changed, rerender, ui_state),
        ]

    rebuild()
    page.show_dialog(ft.AlertDialog(
        title=ft.Row(
            [
                ft.Text(meal.name, size=15, weight=ft.FontWeight.W_600, color=theme.TEXT,
                        expand=True, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                _slot_pill(meal.slot),
            ],
            spacing=8,
        ),
        content=ft.Container(width=420, height=520, content=body),
        actions=[ft.TextButton(content="Close", on_click=lambda ev: page.pop_dialog())],
    ))


def _directions_block(page, state, meal, recipe, ui_state, rerender) -> ft.Column:
    """Real recipe directions from the content catalog when available; AI-
    generated steps only as a fallback for meals without directions (both are
    kept). Scales the ingredient quantities to the household servings."""
    if recipe is not None and recipe.directions:
        header = ft.Text("Directions", size=13, weight=ft.FontWeight.W_600, color=theme.TEXT)
        steps = [
            ft.Text(f"{i}. {step}", size=12.5, color=theme.TEXT)
            for i, step in enumerate(recipe.directions, start=1)
        ]
        note = muted_text(
            f"From {recipe.canonical_name} · serves {recipe.servings} "
            f"(scaled to {meal.servings:g} here).", size=11)
        return ft.Column([header, *steps, note], spacing=4)
    return _steps_block(page, state, meal, ui_state, rerender)


def _steps_block(
    page: ft.Page,
    state: "AppState",
    meal: Meal,
    ui_state: dict,
    refresh: Callable[[], None],
) -> ft.Column:
    """Cooking-steps section of the detail dialog.

    Cached steps render instantly (and offline). Otherwise, with an OpenAI key
    configured, generation starts in the background and the dialog re-renders
    when it lands; the result persists to the recipe cache so a meal only ever
    generates once per signature (portions, servings, restrictions, version).
    """
    header = ft.Text("How to cook", size=13, weight=ft.FontWeight.W_600, color=theme.TEXT)
    request = build_recipe_request(meal, state.profile)
    key = recipe_cache_key(request)

    steps = state.recipes.get(key)
    if steps:
        return ft.Column(
            [
                header,
                *[
                    ft.Text(f"{i}. {step}", size=12.5, color=theme.TEXT)
                    for i, step in enumerate(steps, start=1)
                ],
                muted_text("AI-generated cooking steps — for reference only.", size=12),
            ],
            spacing=4,
        )

    service = get_recipe_service(state.profile, state.http_client, state.foods)
    if service is None:
        return ft.Column(
            [header, muted_text("Add an OpenAI key in Profile to generate cooking steps.", size=12)],
            spacing=4,
        )
    if ui_state.get("recipe_failed"):
        return ft.Column(
            [header, muted_text("Couldn't generate steps right now — reopen this meal to retry.", size=12)],
            spacing=4,
        )

    async def generate() -> None:
        generated = await service.generate(request)
        ui_state["recipe_generating"] = False
        if generated:
            # Keep the steps for the session even if the cache write fails —
            # they are derived data and regenerate at worst.
            state.recipes[key] = generated
            try:
                state.persist(recipes=state.recipes)
            except Exception:  # noqa: BLE001 - cache write is best-effort
                pass
        else:
            ui_state["recipe_failed"] = True
        refresh()

    if not ui_state.get("recipe_generating"):
        ui_state["recipe_generating"] = True
        page.run_task(generate)

    return ft.Column(
        [
            header,
            ft.Row(
                [
                    ft.ProgressRing(width=16, height=16, stroke_width=2, color=theme.PRIMARY),
                    muted_text("Writing cooking steps…", size=12),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        ],
        spacing=6,
    )


def _slider_editor(
    label: str,
    initial_pct: float,
    note: str,
    on_save: Callable[[float, str], None],
    on_cancel: Callable[[], None] | None = None,
    show_note: bool = True,
) -> ft.Column:
    """A drag-to-set 'percent left' editor: slider + live value + Save.

    ``on_save`` receives (fraction_left, note); the note comes from the inline
    field when shown, otherwise the empty string.
    """
    value_text = ft.Text(
        f"{initial_pct:.0f}% left", size=12, weight=ft.FontWeight.W_600,
        color=theme.TEXT, width=70, text_align=ft.TextAlign.RIGHT,
    )
    slider = ft.Slider(
        min=0, max=100, divisions=20, value=float(initial_pct),
        label="{value}% left", active_color=theme.PRIMARY, expand=True,
    )

    def on_change(e) -> None:
        value_text.value = f"{slider.value:.0f}% left"
        value_text.update()

    slider.on_change = on_change
    note_input = ft.TextField(label="Note (optional)", value=note, dense=True, text_size=12.5)
    style_field(note_input)

    buttons: list[ft.Control] = []
    if on_cancel is not None:
        buttons.append(ft.TextButton(content="Cancel", on_click=lambda e: on_cancel()))
    buttons.append(ft.TextButton(
        content="Save",
        on_click=lambda e: on_save(
            float(slider.value) / 100.0,
            (note_input.value or "").strip() if show_note else "",
        ),
    ))

    controls: list[ft.Control] = [
        muted_text(label, size=11.5),
        ft.Row([slider, value_text], spacing=4,
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
    ]
    if show_note:
        controls.append(note_input)
    controls.append(ft.Row(buttons, alignment=ft.MainAxisAlignment.END, spacing=4))
    return ft.Column(controls, spacing=6)


def _note_editor(
    current: str,
    on_save: Callable[[str], None],
    on_cancel: Callable[[], None],
) -> ft.Column:
    note_input = ft.TextField(label="Note", value=current, dense=True, text_size=12.5)
    style_field(note_input)
    return ft.Column(
        [
            muted_text(
                "These leftovers already have history, so only the note can "
                "change — amounts stay as they are.",
                size=11.5,
            ),
            note_input,
            ft.Row(
                [
                    ft.TextButton(content="Cancel", on_click=lambda e: on_cancel()),
                    ft.TextButton(
                        content="Save",
                        on_click=lambda e: on_save((note_input.value or "").strip()),
                    ),
                ],
                alignment=ft.MainAxisAlignment.END,
                spacing=4,
            ),
        ],
        spacing=6,
    )


def _open_leftover_prompt(
    page: ft.Page,
    state: "AppState",
    saved: SavedPlan,
    when: date,
    meal: Meal,
    on_changed: Callable[[], None],
) -> None:
    """Opened right after freshly marking a meal eaten: three unambiguous
    actions, none of which is "close the dialog" doubling as "nothing left"
    (a user closing a dialog is not the same as confirming that).

    Never opened for ready meals / batch leftovers — ``record_leftover``
    itself rejects those (the plan already schedules their leftover); the
    caller gates on the same condition before opening this.
    """

    def save(fraction_left: float, note: str) -> None:
        result = record_leftover(state, saved, when, meal.slot, meal, fraction_left, {}, note)
        if result.message:
            page.show_dialog(ft.SnackBar(ft.Text(result.message)))
        if result.ok:
            page.pop_dialog()
            on_changed()
        else:
            page.update()  # keep the dialog open so the user can retry

    no_leftovers_button = ft.FilledTonalButton(
        content="No leftovers", icon=ft.Icons.CHECK_CIRCLE_OUTLINE, height=34,
        on_click=lambda e: save(0.0, ""),
    )
    slider_block = _slider_editor(
        "Or slide to set how much is left:", 0.0, "", on_save=save,
    )

    page.show_dialog(ft.AlertDialog(
        title=ft.Text(meal.name, size=15, weight=ft.FontWeight.W_600, color=theme.TEXT,
                      max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
        content=ft.Container(
            width=380,
            content=ft.Column(
                [
                    muted_text("Any leftovers from this meal?", size=12.5),
                    ft.Row([no_leftovers_button]),
                    ft.Divider(height=1, color=theme.BORDER),
                    slider_block,
                ],
                spacing=10,
                tight=True,
            ),
        ),
        actions=[ft.TextButton(content="Skip for now", on_click=lambda e: page.pop_dialog())],
    ))


def _tracking_block(
    page: ft.Page,
    state: "AppState",
    saved: SavedPlan,
    when: date,
    meal: Meal,
    on_changed: Callable[[], None],
    rerender: Callable[[], None],
    ui_state: dict,
) -> ft.Column:
    """The tracking section of the meal detail dialog: Eaten / undo / leftover
    reporting (AI note or slider) / inline edits.

    Every action goes through meal_tracking_flow (snapshot → mutate → one
    transaction → rollback on failure); this block only renders state and
    relays results. ``on_changed`` refreshes the meals section AND rebuilds
    the dialog; ``rerender`` rebuilds the dialog only (pure UI mode flips).
    """
    entry = saved.tracking_entry(when, meal.slot)
    eaten = bool(entry.get("eaten"))
    is_ready_meal = (
        meal.prepared_leftover_id is not None or entry.get("linked_leftover_id") is not None
    )

    def handle(result: TrackingResult) -> None:
        if result.message:
            page.show_dialog(ft.SnackBar(ft.Text(result.message)))
        if result.ok:
            ui_state["mode"] = None
            on_changed()
        else:
            page.update()

    def set_mode(mode: str | None) -> None:
        ui_state["mode"] = mode
        rerender()

    # -- not eaten yet -----------------------------------------------------

    if not eaten:
        def handle_eat(e) -> None:
            freshly_prepared = not meal_was_prepared(entry)
            result = prepare_and_eat(state, saved, when, meal.slot, meal)
            if result.message:
                page.show_dialog(ft.SnackBar(ft.Text(result.message)))
            if result.ok:
                ui_state["mode"] = None
                on_changed()
                if freshly_prepared and not (
                    meal.is_leftover or meal.prepared_leftover_id is not None
                ):
                    page.pop_dialog()  # close the detail dialog before opening the prompt
                    _open_leftover_prompt(page, state, saved, when, meal, on_changed)
            else:
                page.update()

        eat_button = ft.FilledTonalButton(
            content="Eaten", icon=ft.Icons.RESTAURANT, height=34,
        )
        eat_button.on_click = handle_eat
        rows: list[ft.Control] = [ft.Row([eat_button])]
        if is_ready_meal:
            rows.append(muted_text("Prepared leftover — uses no pantry ingredients.", size=11.5))
        elif meal.is_leftover:
            rows.append(muted_text("Ingredients were used with the batch dinner.", size=11.5))
        return ft.Column(rows, spacing=6)

    # -- eaten -------------------------------------------------------------

    if can_undo_preparation(state, saved, when, meal.slot, meal):
        fix_button = ft.TextButton(
            content="Undo",
            tooltip="Undo preparation — restores the raw ingredients",
            on_click=lambda e: handle(undo_preparation(state, saved, when, meal.slot, meal)),
        )
    else:
        fix_button = ft.TextButton(
            content="Fix status",
            tooltip=(
                "Correct the display status only — pantry and leftovers stay "
                "untouched, and Eaten won't deduct again"
            ),
            on_click=lambda e: handle(correct_display_status(state, saved, when, meal.slot)),
        )
    rows = [
        ft.Row(
            [
                pill("Eaten ✓", theme.PRIMARY_TINT, theme.PRIMARY_DARK),
                ft.Container(expand=True),
                fix_button,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
    ]

    created_id = entry.get("leftover_created_id")
    record = state.leftovers_by_id.get(created_id) if created_id else None
    if record is not None:
        saved_pct = round(record.initial_fraction_remaining * 100)
        note = entry.get("leftover_note") or record.note
        if ui_state.get("mode") == "edit_amount":
            rows.append(_slider_editor(
                "Slide to correct how much was left:",
                float(saved_pct),
                note,
                on_save=lambda fraction_left, new_note: handle(record_leftover(
                    state, saved, when, meal.slot, meal, fraction_left, {}, new_note,
                )),
                on_cancel=lambda: set_mode(None),
            ))
        elif ui_state.get("mode") == "edit_note":
            rows.append(_note_editor(
                note,
                on_save=lambda new_note: handle(correct_leftover_note(
                    state, saved, when, meal.slot, new_note,
                )),
                on_cancel=lambda: set_mode(None),
            ))
        else:
            summary = f"{100 - saved_pct}% eaten · {saved_pct}% saved"
            if note:
                summary += f" · “{note}”"
            if can_edit_leftover(state, saved, when, meal.slot):
                edit_button = ft.IconButton(
                    icon=ft.Icons.EDIT_OUTLINED, icon_size=16, icon_color=theme.TEXT_MUTED,
                    tooltip="Edit amount eaten",
                    on_click=lambda e: set_mode("edit_amount"),
                )
            else:
                edit_button = ft.IconButton(
                    icon=ft.Icons.EDIT_NOTE, icon_size=16, icon_color=theme.TEXT_MUTED,
                    tooltip="Correct the note (the leftovers already have history)",
                    on_click=lambda e: set_mode("edit_note"),
                )
            rows.append(ft.Row(
                [
                    ft.Text(summary, size=11.5, color=theme.TEXT_MUTED, expand=True,
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, tooltip=summary),
                    edit_button,
                ],
                spacing=2,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ))
    elif is_ready_meal:
        rows.append(muted_text("Prepared leftover — no pantry ingredients used.", size=11.5))
    elif meal.is_leftover:
        rows.append(muted_text("Ingredients were used with the batch dinner.", size=11.5))
    else:
        submitting = {"busy": False}
        note_field = ft.TextField(
            hint_text="Leftovers? e.g. “left about a third”",
            dense=True, text_size=12, expand=True,
        )
        style_field(note_field)
        send_button = ft.IconButton(
            icon=ft.Icons.SEND, icon_size=16, icon_color=theme.PRIMARY,
            tooltip="Analyze the note with AI",
        )

        async def run_ai(note: str) -> None:
            submitting["busy"] = True
            note_field.disabled = True
            send_button.disabled = True
            page.update()
            analyzer = get_leftover_analyzer(state.profile, state.http_client)
            estimate = await analyzer.analyze(meal, note) if analyzer else None
            submitting["busy"] = False
            if estimate is not None:
                # Persist FIRST: the result must land even if the user closed
                # the dialog or switched tabs during the await; only UI work
                # checks the mount state.
                result = record_leftover(
                    state, saved, when, meal.slot, meal,
                    estimate.overall_fraction, dict(estimate.components), note,
                )
                if note_field.page is not None:
                    handle(result)
                return
            if note_field.page is None:
                return  # dead control tree — nothing was persisted, no dialogs
            note_field.disabled = False
            send_button.disabled = False
            _show_toast(page, "AI couldn't estimate that note — set the slider below instead.")
            page.update()

        async def on_submit(e) -> None:
            note = (note_field.value or "").strip()
            if submitting["busy"] or not note:
                return
            if get_leftover_analyzer(state.profile, state.http_client) is None:
                _show_toast(page, "No OpenAI key configured — set the slider below instead.")
                return
            _show_toast(page, "Sending the note and this meal's ingredients to OpenAI…")
            await run_ai(note)

        note_field.on_submit = on_submit
        send_button.on_click = on_submit
        rows.append(muted_text("Any leftovers? Describe them for AI, or set the slider.", size=11.5))
        rows.append(ft.Row(
            [note_field, send_button],
            spacing=2,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ))
        rows.append(_slider_editor(
            "Or slide to set how much is left:",
            0.0,
            "",
            on_save=lambda fraction_left, _unused: handle(record_leftover(
                state, saved, when, meal.slot, meal,
                fraction_left, {}, (note_field.value or "").strip(),
            )),
            show_note=False,
        ))

    return ft.Column(rows, spacing=6)


def _missing_slot_card(slot: MealSlot) -> ft.Container:
    return ft.Container(
        expand=True,
        bgcolor=theme.SURFACE_TINT,
        border_radius=theme.RADIUS_SM,
        padding=12,
        content=ft.Column(
            [
                ft.Row([_slot_pill(slot, active=False)]),
                muted_text("No meal planned — not enough groceries left for this slot.", size=12.5),
            ],
            spacing=8,
        ),
    )


def _day_nutrition_panel(day_nutrients: Nutrients, daily_targets: Nutrients) -> ft.Container:
    """Absolute intake vs. the household's daily targets, one bar per nutrient."""
    rows: list[ft.Control] = [
        muted_text(
            "What this day's planned meals provide vs. the household's daily targets.", size=12
        )
    ]
    for name in Nutrients.NAMES:
        style = NUTRIENT_STYLES[name]
        target = daily_targets.get(name)
        value = day_nutrients.get(name)
        pct = 100.0 * value / target if target > 0 else 100.0
        rows.append(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Row(
                                [
                                    ft.Icon(style.icon, size=15, color=style.color),
                                    ft.Text(
                                        Nutrients.NUTRIENT_LABELS[name],
                                        size=12.5,
                                        weight=ft.FontWeight.W_500,
                                        color=theme.TEXT,
                                    ),
                                ],
                                spacing=6,
                            ),
                            muted_text(
                                f"planned {nutrient_fraction(value, target, name)} · {pct:.0f}%",
                                size=12,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.ProgressBar(
                        value=min(pct / 100.0, 1.0),
                        color=style.color,
                        bgcolor=ft.Colors.with_opacity(0.10, style.color),
                        bar_height=6,
                        border_radius=999,
                    ),
                ],
                spacing=4,
            )
        )
    return ft.Container(
        content=ft.Column(rows, spacing=10),
        border=ft.Border.all(1, theme.BORDER),
        border_radius=theme.RADIUS_SM,
        padding=12,
        visible=False,
    )


def pantry_carryover_block(plan: MealPlan, foods_by_id: dict[str, Food],
                           image_src_for: ImageSrcFor) -> ft.Column | None:
    if not plan.pantry_carryover:
        return None
    rows: list[ft.Control] = [
        ft.Text("Pantry after this plan", size=13.5, weight=ft.FontWeight.W_600, color=theme.TEXT),
        muted_text(
            f"Bought but not scheduled in these {plan.horizon_days} days — "
            "it carries over to your next plan.",
            size=12.5,
        ),
    ]
    for food_id, grams in sorted(plan.pantry_carryover.items()):
        food = foods_by_id.get(food_id)
        if food is None:
            continue
        rows.append(
            ft.Row(
                [
                    food_avatar(food, size=24, image_src=image_src_for(food)),
                    ft.Text(food.name, size=12.5, color=theme.TEXT, expand=True,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    muted_text(carryover_amount_label(food, grams), size=12),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
    return ft.Column(rows, spacing=8)


def daily_meals_section(
    page: ft.Page,
    state: "AppState",
    saved: SavedPlan,
    daily_targets: Nutrients,
    initial_date: date | None = None,
) -> ft.Container:
    plan: MealPlan = saved.meal_plan
    start_date = saved.start_date
    profile: HouseholdProfile = state.profile
    foods_by_id = state.foods_by_id
    image_src_for = state.image_src_for
    today = date.today()
    generated_members = (
        saved.household_snapshot.adults
        + saved.household_snapshot.children
        + saved.household_snapshot.seniors
        if saved.household_snapshot is not None
        else max(profile.total_members, 1)
    )
    is_partial = saved.plan_kind is PlanKind.PARTIAL_FOOD_COVERAGE
    is_stale = saved.profile_stale(profile)
    anchor = initial_date if initial_date is not None else today
    offset = (anchor - start_date).days
    selected = {"index": offset if 0 <= offset < len(plan.days) else 0}

    strip_column = ft.Column(spacing=4)
    detail_column = ft.Column(spacing=12)

    def refresh() -> None:
        # Tracking changed: re-render the day's cards (and the footer states).
        rebuild_detail()
        section.update()

    def select_day(index: int):
        def handler(e) -> None:
            selected["index"] = index
            rebuild_strip()
            rebuild_detail()
            section.update()

        return handler

    def day_cell(index: int, when: date) -> ft.Container:
        is_selected = index == selected["index"]
        ink_color = theme.PRIMARY_DARK if is_selected else theme.TEXT
        return ft.Container(
            width=76,
            height=58,
            ink=True,
            on_click=select_day(index),
            border_radius=theme.RADIUS_SM,
            bgcolor=theme.PRIMARY_TINT if is_selected else theme.SURFACE_TINT,
            border=ft.Border.all(1, theme.PRIMARY) if when == today else None,
            padding=ft.Padding.only(top=10),
            content=ft.Column(
                [
                    ft.Text(
                        when.strftime("%a"),
                        size=11,
                        color=theme.PRIMARY_DARK if is_selected else theme.TEXT_MUTED,
                    ),
                    ft.Text(
                        _fmt_date(when), size=12.5, weight=ft.FontWeight.W_600, color=ink_color
                    ),
                ],
                spacing=2,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def rebuild_strip() -> None:
        rows: list[ft.Control] = []
        for row_start in range(0, len(plan.days), 7):
            rows.append(
                ft.Row(
                    [
                        day_cell(day.day_index, start_date + timedelta(days=day.day_index))
                        for day in plan.days[row_start:row_start + 7]
                    ],
                    spacing=4,
                )
            )
        strip_column.controls = rows

    def rebuild_detail() -> None:
        day = plan.days[selected["index"]]
        when = start_date + timedelta(days=day.day_index)
        day_kcal_per_person = sum(
            meal.per_person_kcal
            if meal.per_person_kcal is not None
            else meal.kcal / generated_members
            for meal in day.meals
        )

        cards: list[ft.Control] = []
        for slot in SLOT_ORDER:
            meal = day.meal_for(slot)
            cards.append(
                meal_card(
                    meal, image_src_for,
                    page=page, state=state, saved=saved, when=when, on_changed=refresh,
                )
                if meal
                else _missing_slot_card(slot)
            )

        day_nutrients = Nutrients()
        for meal in day.meals:
            day_nutrients = day_nutrients.plus(meal.nutrients)
        panel = _day_nutrition_panel(day_nutrients, daily_targets)

        toggle_button = ft.TextButton(
            content="Show day nutrition", icon=ft.Icons.MONITOR_HEART_OUTLINED
        )

        def on_toggle(e) -> None:
            panel.visible = not panel.visible
            toggle_button.content = (
                "Hide day nutrition" if panel.visible else "Show day nutrition"
            )
            detail_column.update()

        toggle_button.on_click = on_toggle

        detail_column.controls = [
            ft.Column(
                [
                    ft.Text(
                        f"{when.strftime('%A')} · {_fmt_date(when)}",
                        size=14.5,
                        weight=ft.FontWeight.W_600,
                        color=theme.TEXT,
                    ),
                    muted_text(
                        f"{len(day.meals)} meals · ≈{day_kcal_per_person:,.0f} kcal/person",
                        size=12.5,
                    ),
                ],
                spacing=2,
            ),
            # Height constant is load-bearing — see MEAL_CARD_ROW_HEIGHT.
            ft.Row(cards, spacing=12, height=MEAL_CARD_ROW_HEIGHT,
                   vertical_alignment=ft.CrossAxisAlignment.STRETCH),
            ft.Row([toggle_button]),
            panel,
        ]

    rebuild_strip()
    rebuild_detail()

    controls: list[ft.Control] = []
    if is_partial:
        controls.append(partial_plan_warning_banner())
    if is_stale:
        controls.append(stale_plan_warning_banner())
    controls.extend([
        muted_text(
            f"Portions were generated for the whole household ({generated_members} "
            f"{'person' if generated_members == 1 else 'people'}). "
            "Pick a day to see its meals.",
            size=12.5,
        ),
        strip_column,
        detail_column,
    ])
    carryover = pantry_carryover_block(plan, foods_by_id, image_src_for)
    if carryover is not None:
        controls.append(ft.Divider(height=1, color=theme.BORDER))
        controls.append(carryover)
    controls.append(
        muted_text("Meals respect your household's dietary restrictions.", size=12)
    )
    section = collapsible_section(
        "Daily meals",
        *controls,
        icon=ft.Icons.RESTAURANT_MENU,
        icon_tooltip="What to cook each day from this basket",
        source_note=(
            "Meals and portions are generated from your basket using standard "
            "grams-per-serving math, not verified recipes."
        ),
        subtitle=f"Named meals for each of the {plan.horizon_days} days, built from your basket",
        expanded=True,
    )
    return section
