"""Pure-logic tests for planning_view helpers (no Flet rendering)."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import flet as ft

from data.loader import load_catalog
from models import (
    DayPlan,
    Meal,
    MealPlan,
    MealPortion,
    MealSlot,
    Pantry,
    PriceSource,
    SavedBasketItem,
    SavedPlan,
)
from models.purchase_log import PurchaseRecord
from services.source_allocation import BuyLine, FoodAllocation, GRAM_EPSILON
from ui.planning_view import (
    ESTIMATED_CAP_DISCLAIMER,
    _basket_section,
    aligned_food_card_grid,
    basket_source_mix,
    compact_package_label,
    package_columns_control,
    package_line_groups,
    pantry_quantity_suffix,
    pantry_coverage_note,
    plan_purchase_groups,
    purchase_input_for_line,
    quantity_rich_text,
    saved_item_quantity_label,
    shopping_quantity_label,
)


class TestPantryCoverageNote:
    def test_zero_from_pantry_is_none(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert pantry_coverage_note(rice, 0.0) is None

    def test_below_epsilon_is_none(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert pantry_coverage_note(rice, GRAM_EPSILON) is None

    def test_dry_grams_food_label(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert pantry_coverage_note(rice, 200.0) == "200 g dry covered by pantry"

    def test_liquid_food_label(self, foods_by_id):
        milk = foods_by_id["milk_whole"]
        assert pantry_coverage_note(milk, 103.0) == "100 ml covered by pantry"


def test_package_need_and_pantry_coverage_share_the_same_unit(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    allocation = FoodAllocation(
        food_id=eggs.id,
        meal_requirement=1800,
        completed=0,
        from_pantry=600,
        gap=1200,
        to_buy=(BuyLine("1 dozen", 600, 2, 6.4, "seed"),),
        covered=600,
    )
    assert shopping_quantity_label(eggs, allocation) == (
        "2 × 1 dozen + 1 dozen in my pantry"
    )


def test_pantry_suffix_respects_threshold_and_food_bound_units(foods_by_id):
    eggs = foods_by_id["eggs_large"]
    milk = foods_by_id["milk_whole"]
    rice = foods_by_id["rice_white"]

    assert pantry_quantity_suffix(eggs, 0.0, "1 dozen") is None
    assert pantry_quantity_suffix(eggs, GRAM_EPSILON, "1 dozen") is None
    assert pantry_quantity_suffix(eggs, 300.0, "1 dozen") == (
        "+ 0.5 dozen in my pantry"
    )
    assert pantry_quantity_suffix(milk, 1949.275, "1 gallon") == (
        "+ 0.5 gallon in my pantry"
    )
    assert pantry_quantity_suffix(rice, 2268.0, "5 lb bag") == (
        "+ 1 5 lb bag in my pantry"
    )


def test_quantity_rich_text_uses_smaller_green_italic_pantry_span():
    text = quantity_rich_text("2 × 1 dozen", "+ 1 dozen in my pantry")

    assert [span.text for span in text.spans] == [
        "2 × 1 dozen",
        "  + 1 dozen in my pantry",
    ]
    assert text.spans[0].style.size == 12.5
    assert text.spans[1].style.size == 11
    assert text.spans[1].style.italic is True
    assert text.spans[1].style.color == "#16A34A"


def _buy_line(label, grams, row_id, package_id, *, count=1, cost=1.0):
    return BuyLine(
        package_label=label,
        package_grams=grams,
        count=count,
        est_cost=cost,
        source="seed_estimate",
        basket_item_id=row_id,
        package_id=package_id,
    )


def test_package_groups_are_two_columns_largest_first_and_wrap():
    stick = _buy_line("1 stick", 113.0, "row-stick", "pkg-stick")
    pound = _buy_line("1 lb (4 sticks)", 454.0, "row-pound", "pkg-pound")
    small = _buy_line("sample", 50.0, "row-small", "pkg-small")

    assert compact_package_label(pound.package_label) == "1 lb"
    assert package_line_groups((stick, pound)) == ((pound, stick),)
    assert package_line_groups((stick, small, pound)) == (
        (pound, stick),
        (small,),
    )


def test_shared_package_frame_keeps_distinct_equal_width_children():
    left = ft.Container(key="row-pound")
    right = ft.Container(key="row-stick")

    shared = package_columns_control((left, right), key="butter-packages")

    assert shared.bgcolor == "#FFFFFF"
    assert isinstance(shared.content, ft.Row)
    assert shared.content.intrinsic_height is True
    assert shared.content.controls[0] is left
    assert isinstance(shared.content.controls[1], ft.VerticalDivider)
    assert shared.content.controls[2] is right
    assert left.expand is True and right.expand is True
    assert left.key != right.key


def test_food_card_grid_preserves_compact_card_heights():
    short = ft.Container(
        key="short",
        width=520,
        height=124,
        alignment=ft.Alignment.TOP_LEFT,
    )
    tall = ft.Container(
        key="tall",
        width=520,
        height=220,
        alignment=ft.Alignment.TOP_LEFT,
    )

    grid = aligned_food_card_grid((short, tall))

    assert isinstance(grid, ft.ResponsiveRow)
    assert grid.vertical_alignment is ft.CrossAxisAlignment.START
    assert grid.spacing == 12
    assert grid.run_spacing == 12
    assert grid.controls == [short, tall]
    assert [card.height for card in grid.controls] == [124, 220]
    for card in grid.controls:
        assert card.width is None
        assert card.alignment == ft.Alignment.TOP_LEFT
        assert card.col == {
            ft.ResponsiveRowBreakpoint.XS: 12,
            ft.ResponsiveRowBreakpoint.LG: 6,
        }


def test_each_package_purchase_payload_keeps_its_own_saved_identity():
    butter = next(food for food in load_catalog() if food.id == "butter")
    packages = {package.label: package for package in butter.package_options}
    pound = _buy_line(
        "1 lb (4 sticks)",
        454.0,
        "row-pound",
        packages["1 lb (4 sticks)"].package_id,
        count=2,
        cost=8.5,
    )
    stick = _buy_line(
        "1 stick",
        113.0,
        "row-stick",
        packages["1 stick"].package_id,
        cost=1.25,
    )

    pound_input = purchase_input_for_line(butter, pound)
    stick_input = purchase_input_for_line(butter, stick)

    assert (
        pound_input.basket_item_id,
        pound_input.package_id,
        pound_input.quantity,
        pound_input.grams,
        pound_input.estimated_line_cost,
    ) == (
        "row-pound",
        packages["1 lb (4 sticks)"].package_id,
        2,
        908.0,
        8.5,
    )
    assert (
        stick_input.basket_item_id,
        stick_input.package_id,
        stick_input.quantity,
        stick_input.grams,
        stick_input.estimated_line_cost,
    ) == (
        "row-stick",
        packages["1 stick"].package_id,
        1,
        113.0,
        1.25,
    )


class _Page:
    def __init__(self):
        self.overlay = []
        self.dialogs = []

    def update(self):
        pass

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()


def _walk(control):
    yield control
    content = getattr(control, "content", None)
    if content is not None:
        yield from _walk(content)
    for child in getattr(control, "controls", None) or ():
        yield from _walk(child)


def _butter_plan(*, historical: bool):
    butter = next(food for food in load_catalog() if food.id == "butter")
    packages = {package.label: package for package in butter.package_options}
    basket = tuple(
        SavedBasketItem(
            basket_item_id=row_id,
            food_id=butter.id,
            package_id=packages[label].package_id,
            package_label=label,
            package_grams=packages[label].grams,
            count=1,
            offer_id=f"offer:{row_id}",
            total_cost_cents=cost_cents,
            unit_cost_cents=cost_cents,
            source=PriceSource.SEED_ESTIMATE.value,
            store="Seed data",
            confidence=1.0,
            match_reason="test",
            matched_product_name=butter.name,
        )
        for label, row_id, cost_cents in (
            ("1 stick", "row-stick", 125),
            ("1 lb (4 sticks)", "row-pound", 425),
        )
    )
    meal = Meal(
        slot=MealSlot.DINNER,
        template_id="",
        name="Butter meal",
        portions=(MealPortion(butter, 1167.0),),
        servings=1.0,
    )
    plan = SavedPlan(
        plan_id="butter-plan",
        start_date=date(2020, 1, 1) if historical else date.today(),
        horizon_days=1,
        created_at="2026-07-15T10:00:00",
        budget=20.0,
        total_cost=5.5,
        meal_plan=MealPlan(days=(DayPlan(0, (meal,)),), horizon_days=1),
        basket=basket,
        consumed_gaps=(),
        pantry_used={butter.id: 600.0} if historical else {},
    )
    state = SimpleNamespace(
        saved_plan=plan,
        profile=None,
        foods_by_id={butter.id: butter},
        pantry=Pantry(items={butter.id: 600.0}),
        purchase_log=[],
        purchase_log_error=None,
        image_src_for=lambda food: None,
        persist=lambda **kwargs: None,
    )
    return butter, plan, state


def test_active_dual_package_card_has_one_suffix_and_larger_package_left():
    _, plan, state = _butter_plan(historical=False)
    section = _basket_section(_Page(), plan, state)
    controls = list(_walk(section))
    group = next(
        control
        for control in controls
        if str(getattr(control, "key", "")).startswith("package-group:butter:")
    )

    assert group.content.controls[0].key == "row-pound"
    assert group.content.controls[2].key == "row-stick"
    butter_card = next(
        control
        for control in controls
        if getattr(control, "key", None) == "food-card:butter"
    )
    assert butter_card.height == 124
    pound_column = next(
        control
        for control in controls
        if getattr(control, "key", None) == "row-pound"
    )
    assert pound_column.height == 50
    assert isinstance(pound_column.content, ft.Row)
    assert isinstance(pound_column.content.controls[0], ft.Column)
    assert isinstance(pound_column.content.controls[1], ft.IconButton)
    assert pound_column.content.controls[2].value == "$4.25"
    assert isinstance(pound_column.content.controls[3], ft.FilledTonalButton)
    assert pound_column.content.controls[3].height == 28
    assert pound_column.padding.top == 4
    assert pound_column.padding.bottom == 4
    assert [
        control.key
        for control in controls
        if isinstance(control, ft.FilledTonalButton)
        and str(getattr(control, "key", "")).startswith("purchase:")
    ] == ["purchase:row-pound", "purchase:row-stick"]
    pantry_spans = [
        span
        for control in controls
        for span in (getattr(control, "spans", None) or ())
        if "in my pantry" in (span.text or "")
    ]
    assert len(pantry_spans) == 1


def test_historical_dual_package_card_uses_frozen_pantry_block_without_suffix():
    _, plan, state = _butter_plan(historical=True)
    section = _basket_section(_Page(), plan, state)
    controls = list(_walk(section))

    assert any(
        isinstance(control, ft.Text) and control.value == "Used from pantry"
        for control in controls
    )
    assert not any(
        "in my pantry" in (span.text or "")
        for control in controls
        for span in (getattr(control, "spans", None) or ())
    )
    assert any(
        str(getattr(control, "key", "")).startswith("snapshot-package-group:butter:")
        for control in controls
    )


def _basket_item(food, *, row_id: str, source: PriceSource, grams: float = 500.0):
    package = food.package_options[0]
    return SavedBasketItem(
        basket_item_id=row_id,
        food_id=food.id,
        package_id=package.package_id,
        package_label="Frozen package label",
        package_grams=grams,
        count=1,
        offer_id=f"offer:{row_id}",
        total_cost_cents=250,
        unit_cost_cents=250,
        source=source.value,
        store="Test store",
        confidence=1.0,
        match_reason="test",
        matched_product_name=food.name,
    )


def _plan(basket=()):
    return SavedPlan(
        plan_id="plan-one",
        start_date=date(2026, 7, 14),
        horizon_days=7,
        created_at="2026-07-14T10:00:00",
        budget=100.0,
        total_cost=sum(item.cost for item in basket),
        meal_plan=MealPlan(days=(), horizon_days=7),
        basket=tuple(basket),
        consumed_gaps=(),
    )


def _record(event_id: str, group_id: str, plan_id: str | None):
    return PurchaseRecord(
        event_id=event_id,
        food_id="rice_white",
        raw_name="Rice",
        brand=None,
        package_label="500 g",
        grams=500.0,
        quantity=1,
        line_total=None,
        estimated_line_cost=2.5,
        price_source="unknown",
        store="",
        photo_path=None,
        group_id=group_id,
        origin="direct_button",
        purchased_at="2026-07-14T10:00:00",
        plan_id=plan_id,
        pantry_grams_before=0.0,
    )


def test_source_mix_counts_every_offer_not_distinct_food(foods_by_id):
    rice = foods_by_id["rice_white"]
    items = (
        _basket_item(rice, row_id="row-a", source=PriceSource.KROGER_REAL_PRICE),
        _basket_item(rice, row_id="row-b", source=PriceSource.BLS_REGIONAL_AVERAGE),
    )

    assert basket_source_mix(items) == {
        PriceSource.KROGER_REAL_PRICE: 1,
        PriceSource.BLS_REGIONAL_AVERAGE: 1,
    }


def test_purchase_history_keeps_every_non_void_event_group():
    plan = _plan()
    first = _record("event-a", "group-a", plan.plan_id)
    second = _record("event-b", "group-b", plan.plan_id)
    same_group = _record("event-c", "group-b", plan.plan_id)
    pantry_only_same_group = _record("event-c2", "group-b", None)
    voided = replace(_record("event-d", "group-c", plan.plan_id), voided_at="later")
    off_plan = _record("event-e", "group-d", None)

    assert plan_purchase_groups(
        plan, [first, second, same_group, pantry_only_same_group, voided, off_plan]
    ) == (
        ("group-a", (first,)),
        ("group-b", (second, same_group, pantry_only_same_group)),
    )


def test_historical_quantity_uses_saved_package_grams(foods_by_id):
    rice = foods_by_id["rice_white"]
    item = _basket_item(
        rice,
        row_id="snapshot-row",
        source=PriceSource.SEED_ESTIMATE,
        grams=1234.0,
    )

    label = saved_item_quantity_label(rice, item)

    assert label.startswith("Frozen package label · ")
    assert label.endswith(" each")
    assert "kg" in label


def test_estimated_cap_disclaimer_names_checkout_exclusions():
    for term in (
        "mix stores",
        "tax",
        "delivery fees",
        "minimum-order",
        "membership",
        "price changes",
    ):
        assert term in ESTIMATED_CAP_DISCLAIMER
