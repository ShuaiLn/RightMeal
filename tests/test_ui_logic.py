"""Pure-logic tests for UI helpers (no Flet rendering)."""

from datetime import date

import pytest

from models import MealSlot, Pantry
from models.purchase_log import PurchaseInput, new_purchase_event_id
from services.meal_tracking_flow import reserved_slot_for
from services.pantry_flow import (
    can_void_group,
    latest_group_for_food,
    record_purchase_event,
)
from ui.pantry_view import (
    AddAction,
    apply_catalog_add,
    resolve_add_target,
    resolve_food,
    sorted_catalog_foods,
)

WHEN = date(2026, 7, 7)


class TestResolveFood:
    def test_selection_wins_while_text_matches(self, foods, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert resolve_food(rice.name, rice.id, foods, foods_by_id) is rice
        assert resolve_food("", rice.id, foods, foods_by_id) is rice

    def test_stale_selection_ignored_after_retyping(self, foods, foods_by_id):
        rice = foods_by_id["rice_white"]
        # The user picked rice, then typed something else entirely.
        assert resolve_food("bananas", rice.id, foods, foods_by_id) is not rice

    def test_exact_name_match_normalized(self, foods, foods_by_id):
        rice = foods_by_id["rice_white"]
        assert resolve_food(f"  {rice.name.upper()}  ", None, foods, foods_by_id) is rice

    def test_ambiguous_partial_never_autopicks(self, foods, foods_by_id):
        # "Chicken" prefixes both chicken foods — must not pick either.
        chicken_foods = [f for f in foods if "chicken" in f.name.lower()]
        assert len(chicken_foods) >= 2
        assert resolve_food("Chicken", None, foods, foods_by_id) is None

    def test_garbage_returns_none(self, foods, foods_by_id):
        assert resolve_food("definitely not a food", None, foods, foods_by_id) is None
        assert resolve_food("", None, foods, foods_by_id) is None


class TestResolveAddTarget:
    def test_resolve_add_target_finds_already_stocked_food_by_exact_name(
        self, foods, foods_by_id
    ):
        rice = foods_by_id["rice_white"]
        pantry = Pantry(items={rice.id: 100.0})
        decision = resolve_add_target(
            rice.name, None, sorted_catalog_foods(foods, pantry.items), foods_by_id,
        )
        assert decision.action is AddAction.CATALOG
        assert decision.food is rice

    def test_resolve_add_target_finds_already_stocked_food_by_alias(
        self, foods, foods_by_id
    ):
        beans = foods_by_id["black_beans_dry"]
        pantry = Pantry(items={beans.id: 50.0})
        decision = resolve_add_target(
            "kidney beans", None, sorted_catalog_foods(foods, pantry.items), foods_by_id,
        )
        assert decision.action is AddAction.CATALOG
        assert decision.food is beans

    def test_resolve_add_target_ignores_stale_selected_key_after_retyping(
        self, foods, foods_by_id
    ):
        """The user picked rice from the dropdown, then retyped over it —
        dispatch must follow resolve_food's guard, not the stale selection."""
        rice = foods_by_id["rice_white"]
        beans = foods_by_id["black_beans_dry"]
        decision = resolve_add_target(
            beans.name, rice.id, sorted_catalog_foods(foods), foods_by_id,
        )
        assert decision.action is AddAction.CATALOG
        assert decision.food is beans

    def test_resolve_add_target_medium_confidence_disambiguates(self, foods, foods_by_id):
        # "fillet" is close between two seed foods -- ambiguous, must not auto-pick.
        decision = resolve_add_target("fillet", None, sorted_catalog_foods(foods), foods_by_id)
        assert decision.action is AddAction.DISAMBIGUATE
        ids = {c.food_id for c in decision.candidates}
        assert "salmon_fillet" in ids and "tilapia_fillet" in ids

    def test_resolve_add_target_no_match_is_custom(self, foods, foods_by_id):
        decision = resolve_add_target(
            "xyzzy nonsense", None, sorted_catalog_foods(foods), foods_by_id,
        )
        assert decision.action is AddAction.CUSTOM
        assert decision.food is None
        assert decision.candidates == ()


class TestApplyCatalogAdd:
    def test_pantry_add_accumulates_existing_stock(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        pantry = Pantry()
        apply_catalog_add(pantry, rice.id, 100.0)
        total = apply_catalog_add(pantry, rice.id, 485.0)
        assert total == pytest.approx(585.0)
        assert pantry.items[rice.id] == pytest.approx(585.0)
        assert pantry.custom_items == []

    def test_pantry_add_accumulates_fractional_amounts(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        pantry = Pantry()
        apply_catalog_add(pantry, rice.id, 100.0)
        total = apply_catalog_add(pantry, rice.id, 0.5)
        assert total == pytest.approx(100.5)


class TestPurchasedPartition:
    def test_purchased_map_partitions_basket(self, foods_by_id):
        from test_pantry_flow import make_basket_item, make_plan

        rice = foods_by_id["rice_white"]
        oil = foods_by_id["canola_oil"]
        plan = make_plan([
            make_basket_item(rice, rice.package_options[0], 1),
            make_basket_item(oil, oil.package_options[0], 1),
        ])
        grouped = {item.food_id: [item] for item in plan.basket}
        pantry = Pantry()
        log = []
        event_id = new_purchase_event_id()
        record_purchase_event(plan, pantry, log, PurchaseInput(
            event_id=event_id, food_id=rice.id, grams=500.0,
            apply_to_plan=True, group_id=event_id,
        ))
        to_buy = [fid for fid in grouped if fid not in plan.purchased]
        done = [fid for fid in grouped if fid in plan.purchased]
        assert to_buy == [oil.id]
        assert done == [rice.id]
        group_id = latest_group_for_food(log, rice.id)
        assert can_void_group(plan, pantry, log, group_id, today=WHEN)[0]


class TestStaleTracking:
    def test_new_plan_starts_with_empty_tracking(self, foods_by_id):
        from test_pantry_flow import make_plan

        # Regeneration replaces the whole SavedPlan: tracking and reservations
        # start empty, so no slot can inherit another meal's history.
        plan = make_plan()
        assert plan.tracking == {}
        assert plan.leftovers_used == {}
        entry = plan.tracking_entry(WHEN, MealSlot.DINNER)
        assert entry["eaten"] is False
        assert entry["leftover_created_id"] is None

    def test_reserved_slot_for_none_cases(self, foods_by_id):
        from test_pantry_flow import make_plan

        plan = make_plan()
        assert reserved_slot_for(None, "lo1") is None
        assert reserved_slot_for(plan, "lo1") is None  # not reserved


class _DummyPage:
    """Stands in for ft.Page: hashable, weakref-able, with an overlay list
    and a no-op update() so view-builders that call page.update() during
    construction don't need a real Flet page to run against."""

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


def _iter_controls(node):
    """Depth-first walk of a Flet control tree via its two containment
    attributes (``content`` for Container/TextButton-like wrappers,
    ``controls`` for Row/Column) — the only two build_start_view's tree uses."""
    yield node
    content = getattr(node, "content", None)
    if content is not None:
        yield from _iter_controls(content)
    for child in getattr(node, "controls", None) or []:
        yield from _iter_controls(child)


def _find_control(root, predicate):
    for node in _iter_controls(root):
        if predicate(node):
            return node
    raise AssertionError("no matching control found in tree")


def test_candidate_cap_conversion_never_rounds_below_candidate():
    from ui.start_view import _base_budget_for_cap

    daily = _base_budget_for_cap(10.00, 3, daily_mode=True)
    weekly = _base_budget_for_cap(10.00, 3, daily_mode=False)
    assert round(daily * 3, 2) >= 10.00
    assert round(weekly * 3 / 7, 2) >= 10.00


class TestStartViewDraftPreservation:
    """Control-tree test: build the real Start view, mutate its Budget/ZIP
    controls, trigger the Edit button, and confirm the draft survives a full
    rebuild — the actual round trip through the Profile page in app.py."""

    def _build(self, state, edit_calls):
        import flet as ft

        from ui.start_view import build_start_view

        page = _DummyPage()
        root = build_start_view(
            page,
            state,
            on_planned=lambda: None,
            on_edit_household=lambda: edit_calls.append(1),
        )
        return root, ft

    def _fields(self, root, ft):
        budget_field = _find_control(
            root,
            lambda c: isinstance(c, ft.TextField)
            and c.label == "Estimated basket budget cap",
        )
        zip_field = _find_control(
            root, lambda c: isinstance(c, ft.TextField) and c.label == "ZIP code"
        )
        edit_button = _find_control(
            root, lambda c: isinstance(c, ft.TextButton) and c.content == "Edit household"
        )
        return budget_field, zip_field, edit_button

    def _make_state(self, tmp_path, **profile_kwargs):
        from models import HouseholdProfile
        from services.profile_store import ProfileStore
        from ui.state import AppState

        state = AppState(store=ProfileStore(tmp_path))
        state.profile = HouseholdProfile(**profile_kwargs)
        return state

    def test_edit_preserves_and_restores_budget_and_zip(self, tmp_path):
        state = self._make_state(tmp_path, adults=2, children=1, zip_code="90001")

        edit_calls = []
        root, ft = self._build(state, edit_calls)
        budget_field, zip_field, edit_button = self._fields(root, ft)

        budget_field.value = "77"
        zip_field.value = "94103"
        edit_button.on_click(None)

        assert edit_calls == [1]  # on_edit_household called exactly once

        draft = state.plan_draft
        assert draft is not None
        assert draft.budget_text == "77"
        assert draft.zip_text == "94103"
        assert draft.zip_dirty is True  # "94103" != profile.zip_code "90001"
        assert draft.mode == "weekly"
        assert draft.variety == "balanced"

        # Rebuild, as app.py does when navigating back from Profile.
        root2, _ = self._build(state, [])
        budget_field2, zip_field2, _ = self._fields(root2, ft)
        assert budget_field2.value == "77"
        assert zip_field2.value == "94103"

    def test_unedited_zip_tracks_profile_after_rebuild(self, tmp_path):
        """If the user never touched ZIP, a rebuild should reflect whatever
        the profile's ZIP is at that point (e.g. changed on the Profile
        page), not a stale value frozen at Edit time."""
        state = self._make_state(tmp_path, adults=1, zip_code="10001")

        root, ft = self._build(state, [])
        budget_field, zip_field, edit_button = self._fields(root, ft)
        budget_field.value = "30"
        edit_button.on_click(None)

        assert state.plan_draft.zip_dirty is False

        state.profile.zip_code = "20002"  # simulated Profile-page edit
        root2, _ = self._build(state, [])
        _, zip_field2, _ = self._fields(root2, ft)
        assert zip_field2.value == "20002"


def _make_app_state(tmp_path, **profile_kwargs):
    from models import HouseholdProfile
    from services.profile_store import ProfileStore
    from ui.state import AppState

    state = AppState(store=ProfileStore(tmp_path))
    state.profile = HouseholdProfile(**profile_kwargs)
    return state


class TestGenerationToken:
    def test_second_begin_invalidates_the_first(self, tmp_path):
        state = _make_app_state(tmp_path, adults=1, zip_code="90001")
        first = state.begin_generation()
        assert state.is_current_generation(first)
        second = state.begin_generation()
        assert not state.is_current_generation(first)
        assert state.is_current_generation(second)
        assert second == first + 1

    def test_persist_pantry_bumps_but_plan_does_not(self, tmp_path):
        from test_pantry_flow import make_plan

        state = _make_app_state(tmp_path, adults=1, zip_code="90001")
        gen = state.begin_generation()
        state.persist(plan=make_plan())
        assert state.is_current_generation(gen)  # a plan write is not an input edit
        state.persist(pantry=Pantry())
        assert not state.is_current_generation(gen)  # the pantry is an input

    def test_profile_snapshot_is_isolated_from_live_edits(self):
        import copy

        from models import HouseholdProfile

        profile = HouseholdProfile(adults=2, allergies=["peanut"], zip_code="90001")
        snapshot = copy.deepcopy(profile)
        profile.adults = 5
        profile.allergies.append("milk")
        assert snapshot.adults == 2
        assert snapshot.allergies == ["peanut"]


class TestStaleGenerationRace:
    """Blocker-1 regression: an old generation finishing AFTER the user edited
    inputs (and possibly started a new generation) must never persist."""

    def _build_racy_view(self, tmp_path, monkeypatch, seed_quotes, gates, calls):
        import asyncio

        import flet as ft

        from planner.recipe_scheduler import RepairStats
        from services.planner_engine import RecipeFirstOutput
        from ui import start_view

        from conftest import make_recipe_meal_plan, result_from_demand

        state = _make_app_state(
            tmp_path, adults=2, children=2, city="LA", zip_code="90001"
        )

        class StubEngine:
            async def price_all(self, foods, location, on_progress=None):
                await asyncio.sleep(0)
                return dict(seed_quotes)

        monkeypatch.setattr(start_view, "build_engine", lambda s: StubEngine())

        def fake_generate(recipes, foods_by_id, profile, nutrition, pantry, quotes,
                          budget, horizon, variety_mode, preassigned=(), config=None):
            index = len(calls)
            calls.append(budget)
            gates[index].wait(timeout=10)  # runs in the to_thread worker
            demand = {"rice_white": 1000.0, "eggs_large": 600.0}
            result = result_from_demand(
                demand, profile, budget, horizon, foods_by_id, quotes, nutrition
            )
            plan = make_recipe_meal_plan(foods_by_id, horizon=horizon)
            return RecipeFirstOutput(plan, result, (), variety_mode, RepairStats())

        monkeypatch.setattr(start_view, "generate_recipe_first", fake_generate)

        planned: list[int] = []
        page = _DummyPage()
        root = start_view.build_start_view(
            page, state,
            on_planned=lambda: planned.append(1),
            on_edit_household=lambda: None,
        )
        budget_field = _find_control(
            root,
            lambda c: isinstance(c, ft.TextField)
            and c.label == "Estimated basket budget cap",
        )
        plan_button = _find_control(
            root, lambda c: isinstance(c, ft.FilledButton) and c.content == "Plan my groceries"
        )
        return state, budget_field, plan_button.on_click, planned

    async def test_stale_generation_never_persists(self, tmp_path, monkeypatch, seed_quotes):
        import asyncio
        import threading

        gates = [threading.Event(), threading.Event()]
        calls: list[float] = []
        state, budget_field, handler, planned = self._build_racy_view(
            tmp_path, monkeypatch, seed_quotes, gates, calls
        )

        budget_field.value = "50"
        task1 = asyncio.ensure_future(handler(None))
        while len(calls) < 1:  # first generation is now blocked in its worker
            await asyncio.sleep(0.01)

        budget_field.value = "77"
        task2 = asyncio.ensure_future(handler(None))
        while len(calls) < 2:
            await asyncio.sleep(0.01)

        gates[1].set()  # the FRESH generation finishes first ...
        await task2
        gates[0].set()  # ... then the stale one, which must be discarded
        await task1

        assert calls == [50.0, 77.0]
        assert planned == [1]  # only the fresh run reached on_planned
        assert state.saved_plan is not None and state.saved_plan.budget == 77.0
        loaded = state.plan_store.load(state.foods_by_id)
        assert loaded is not None and loaded.budget == 77.0  # disk matches

    async def test_pantry_edit_mid_flight_discards_result(
        self, tmp_path, monkeypatch, seed_quotes
    ):
        """The worse variant: the user edits an input mid-flight but never
        starts a second generation — the old result must still be discarded."""
        import asyncio
        import threading

        gates = [threading.Event()]
        calls: list[float] = []
        state, budget_field, handler, planned = self._build_racy_view(
            tmp_path, monkeypatch, seed_quotes, gates, calls
        )

        budget_field.value = "50"
        task = asyncio.ensure_future(handler(None))
        while len(calls) < 1:
            await asyncio.sleep(0.01)

        state.persist(pantry=Pantry())  # simulated pantry edit mid-generation
        gates[0].set()
        await task

        assert planned == []
        assert state.saved_plan is None
        assert state.plan_store.load(state.foods_by_id) is None


class TestStartViewHousehold:
    def test_zero_member_profile_disables_plan_button(self, tmp_path):
        import flet as ft

        from ui.start_view import build_start_view

        state = _make_app_state(tmp_path, adults=0, zip_code="90001")
        root = build_start_view(
            _DummyPage(), state, on_planned=lambda: None, on_edit_household=lambda: None
        )
        plan_button = _find_control(
            root, lambda c: isinstance(c, ft.FilledButton) and c.content == "Plan my groceries"
        )
        assert plan_button.disabled is True
        _find_control(
            root,
            lambda c: isinstance(c, ft.Text)
            and c.value == "Add at least one household member to plan meals.",
        )

    def test_household_summary_rendered_near_edit_button(self, tmp_path):
        import flet as ft

        from ui.plan_draft import household_summary_text
        from ui.start_view import build_start_view

        state = _make_app_state(tmp_path, adults=2, children=1, zip_code="90001")
        root = build_start_view(
            _DummyPage(), state, on_planned=lambda: None, on_edit_household=lambda: None
        )
        summary = _find_control(
            root,
            lambda c: isinstance(c, ft.Text)
            and c.value == household_summary_text(state.profile),
        )
        assert summary.value == "3 servings (2 adults, 1 child)"
        plan_button = _find_control(
            root, lambda c: isinstance(c, ft.FilledButton) and c.content == "Plan my groceries"
        )
        assert not plan_button.disabled

    def test_draft_restore_populates_fields(self, tmp_path):
        import flet as ft
        from datetime import timedelta

        from ui.plan_draft import PlanDraft
        from ui.start_view import build_start_view

        state = _make_app_state(tmp_path, adults=2, zip_code="90001")
        today = date.today()
        state.plan_draft = PlanDraft(
            budget_text="66", zip_text="94000", zip_dirty=True, mode="daily",
            variety="meal_prep", start_date=today, end_date=today + timedelta(days=2),
        )
        root = build_start_view(
            _DummyPage(), state, on_planned=lambda: None, on_edit_household=lambda: None
        )
        budget_field = _find_control(
            root,
            lambda c: isinstance(c, ft.TextField)
            and c.label == "Estimated basket budget cap",
        )
        zip_field = _find_control(
            root, lambda c: isinstance(c, ft.TextField) and c.label == "ZIP code"
        )
        assert budget_field.value == "66"
        assert zip_field.value == "94000"


class TestStartViewDeletePlan:
    def test_delete_button_only_appears_for_existing_plan(self, tmp_path):
        import flet as ft

        from test_pantry_flow import make_plan
        from ui.start_view import build_start_view

        state = _make_app_state(tmp_path, adults=1, zip_code="90001")
        page = _DummyPage()
        without_plan = build_start_view(
            page, state, on_planned=lambda: None, on_edit_household=lambda: None
        )
        assert not any(
            isinstance(control, ft.OutlinedButton) and control.content == "Delete plan"
            for control in _iter_controls(without_plan)
        )

        state.saved_plan = make_plan()
        with_plan = build_start_view(
            page, state, on_planned=lambda: None, on_edit_household=lambda: None
        )
        _find_control(
            with_plan,
            lambda control: isinstance(control, ft.OutlinedButton)
            and control.content == "Delete plan",
        )

    def test_delete_requires_confirmation_and_calls_handler(self, tmp_path):
        import flet as ft

        from test_pantry_flow import make_plan
        from ui.start_view import build_start_view

        state = _make_app_state(tmp_path, adults=1, zip_code="90001")
        state.saved_plan = make_plan()
        page = _DummyPage()
        deleted = []
        root = build_start_view(
            page,
            state,
            on_planned=lambda: None,
            on_edit_household=lambda: None,
            on_delete_plan=lambda: deleted.append(1),
        )
        delete_button = _find_control(
            root,
            lambda control: isinstance(control, ft.OutlinedButton)
            and control.content == "Delete plan",
        )

        delete_button.on_click(None)
        assert deleted == []
        assert len(page.dialogs) == 1
        dialog = page.dialogs[-1]
        confirm = next(
            action
            for action in dialog.actions
            if isinstance(action, ft.FilledButton) and action.content == "Delete plan"
        )
        confirm.on_click(None)

        assert deleted == [1]
        assert page.dialogs == []


class TestProfileClearAllData:
    def test_clear_all_data_requires_explicit_confirmation(self):
        import flet as ft

        from models import HouseholdProfile
        from ui.profile_view import build_profile_view

        page = _DummyPage()
        cleared = []
        root = build_profile_view(
            page,
            HouseholdProfile(adults=1, zip_code="90001"),
            on_save=lambda profile: None,
            on_delete=lambda: cleared.append(1),
        )
        clear_button = _find_control(
            root,
            lambda control: isinstance(control, ft.OutlinedButton)
            and control.content == "Clear all user data",
        )

        clear_button.on_click(None)
        assert cleared == []
        assert len(page.dialogs) == 1
        cancel = next(
            action
            for action in page.dialogs[-1].actions
            if isinstance(action, ft.TextButton) and action.content == "Cancel"
        )
        cancel.on_click(None)
        assert cleared == []
        assert page.dialogs == []

        clear_button.on_click(None)
        confirm = next(
            action
            for action in page.dialogs[-1].actions
            if isinstance(action, ft.FilledButton)
            and action.content == "Clear all user data"
        )
        confirm.on_click(None)

        assert cleared == [1]
        assert page.dialogs == []


class TestEnsureFilePicker:
    def test_picker_is_a_service_not_an_overlay_control(self):
        import flet as ft

        from ui import photo_purchase

        page = _DummyPage()
        picker = photo_purchase.ensure_file_picker(page)
        assert isinstance(picker, ft.FilePicker)
        # Flet 0.85 renders overlay-hosted services as a red "Unknown
        # control" box — the picker must never be added there.
        assert page.overlay == []
        # Flet prunes a service once <2 strong refs remain outside its
        # registry, so the module slot must hold the current picker.
        assert photo_purchase._pickers[page] is picker

    def test_rebuild_replaces_slot_so_stale_picker_gets_pruned(self):
        from ui import photo_purchase

        page = _DummyPage()
        first = photo_purchase.ensure_file_picker(page)
        second = photo_purchase.ensure_file_picker(page)
        assert second is not first
        assert photo_purchase._pickers[page] is second
