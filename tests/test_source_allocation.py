"""Live source-allocation invariants: meals/nutrition frozen, sourcing live."""

from datetime import date, timedelta

import pytest

from models import Meal, MealPlan, MealPortion, MealSlot, Pantry, SavedBasketItem, SavedPlan
from models.meals import DayPlan
from services.source_allocation import (
    allocate_sources,
    covered_nutrients,
    dynamic_open_cost,
    is_historical,
)

WHEN = date(2026, 7, 7)


def make_meal(portions, slot=MealSlot.DINNER, is_leftover=False, batch_id=None) -> Meal:
    return Meal(
        slot=slot,
        template_id="t",
        name="Test meal",
        portions=tuple(portions),
        is_leftover=is_leftover,
        batch_id=batch_id,
    )


def make_basket_item(food, package, count, cost) -> SavedBasketItem:
    return SavedBasketItem(
        food_id=food.id,
        package_label=package.label,
        count=count,
        cost=cost,
        source="seed_estimate",
        store="Seed data",
        confidence=1.0,
        match_reason="test",
        matched_product_name=food.name,
    )


def make_plan(days=(), basket=(), pantry_used=None, start=WHEN, horizon=7) -> SavedPlan:
    plan = SavedPlan(
        start_date=start,
        horizon_days=horizon,
        created_at="2026-07-07T09:00:00",
        budget=50.0,
        total_cost=0.0,
        meal_plan=MealPlan(days=tuple(days), horizon_days=horizon),
        basket=tuple(basket),
        consumed_gaps=(),
    )
    if pantry_used:
        plan.pantry_used.update(pantry_used)
    return plan


@pytest.fixture
def rice(foods_by_id):
    return foods_by_id["rice_white"]


class TestCompletedMeals:
    def test_leftover_cook_never_reenters_need_to_buy(self, rice, foods_by_id):
        """300 g fully cooked; 60% eaten / 40% saved as leftovers — the 120 g
        is cooked food, never raw requirement again."""
        meal = make_meal([MealPortion(rice, 300.0)])
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))])
        plan.set_prepared(WHEN, MealSlot.DINNER, True)
        plan.set_tracking(WHEN, MealSlot.DINNER, eaten=True, leftover_note="left 40%")
        plan.set_ingredients_used(WHEN, MealSlot.DINNER, 1.0, {rice.id: 300.0})

        allocations = allocate_sources(plan, Pantry(), foods_by_id)
        alloc = allocations[rice.id]
        assert alloc.completed == pytest.approx(300.0)
        assert alloc.gap == 0.0
        assert alloc.to_buy == ()

    def test_clamped_eat_completes_full_requirement(self, rice, foods_by_id):
        """Stock was 100 g short of the 300 g draw; the meal was still cooked —
        the 200 g shortfall never enters Need to buy."""
        meal = make_meal([MealPortion(rice, 300.0)])
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))])
        plan.set_prepared(WHEN, MealSlot.DINNER, True)
        plan.set_ingredients_used(WHEN, MealSlot.DINNER, 1.0, {rice.id: 100.0})  # clamped

        allocations = allocate_sources(plan, Pantry(), foods_by_id)
        alloc = allocations[rice.id]
        assert alloc.completed == pytest.approx(300.0)
        assert alloc.gap == 0.0

    def test_eaten_draws_never_double_count_after_pantry_edit(self, rice, foods_by_id):
        """Eat one of two meals (stock deducted), then delete the pantry food:
        the gap is only the unprepared meal's draw, never the eaten grams."""
        eaten = make_meal([MealPortion(rice, 300.0)], slot=MealSlot.LUNCH)
        future = make_meal([MealPortion(rice, 300.0)], slot=MealSlot.DINNER)
        plan = make_plan(days=[DayPlan(day_index=0, meals=(eaten, future))])
        plan.set_prepared(WHEN, MealSlot.LUNCH, True)
        plan.set_ingredients_used(WHEN, MealSlot.LUNCH, 1.0, {rice.id: 300.0})

        pantry = Pantry()  # user then deleted all remaining stock
        alloc = allocate_sources(plan, pantry, foods_by_id)[rice.id]
        assert alloc.meal_requirement == pytest.approx(600.0)
        assert alloc.completed == pytest.approx(300.0)
        assert alloc.gap == pytest.approx(300.0)


class TestPantryEdits:
    def _plan_and_pkg(self, rice):
        pkg = rice.package_options[0]
        meal = make_meal([MealPortion(rice, pkg.grams * 2)])
        basket = [make_basket_item(rice, pkg, 2, cost=3.0)]
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))], basket=basket)
        return plan, pkg

    def test_pantry_shrink_grows_gap_with_rounded_packages(self, rice, foods_by_id):
        plan, pkg = self._plan_and_pkg(rice)
        stocked = Pantry()
        stocked.add(rice.id, pkg.grams * 2)  # fully covered by pantry
        assert allocate_sources(plan, stocked, foods_by_id)[rice.id].to_buy == ()

        half = Pantry()
        half.add(rice.id, pkg.grams * 0.5)  # gap of 1.5 packages -> round up to 2
        alloc = allocate_sources(plan, half, foods_by_id)[rice.id]
        assert alloc.gap == pytest.approx(pkg.grams * 1.5)
        assert [(line.package_label, line.count) for line in alloc.to_buy] == [(pkg.label, 2)]
        assert dynamic_open_cost({rice.id: alloc}) == pytest.approx(3.0)

    def test_pantry_grow_shrinks_need_to_buy(self, rice, foods_by_id):
        plan, pkg = self._plan_and_pkg(rice)
        partial = Pantry()
        partial.add(rice.id, pkg.grams)  # one package's worth already at home
        alloc = allocate_sources(plan, partial, foods_by_id)[rice.id]
        assert [(line.package_label, line.count) for line in alloc.to_buy] == [(pkg.label, 1)]
        assert alloc.from_pantry == pytest.approx(pkg.grams)

    def test_pantry_edits_never_touch_purchase_history(self, rice, foods_by_id):
        plan, pkg = self._plan_and_pkg(rice)
        plan.purchased[rice.id] = pkg.grams
        before = dict(plan.purchased)
        allocate_sources(plan, Pantry(), foods_by_id)
        assert plan.purchased == before
        assert plan.pantry_used == {}


class TestFreshPlanIdentity:
    def test_untouched_plan_reproduces_original_basket(self, rice, foods_by_id):
        pkg = rice.package_options[0]
        # The plan drew 500 g from the pantry and bought 2 packages for the rest.
        meal = make_meal([MealPortion(rice, 500.0 + pkg.grams * 2)])
        basket = [make_basket_item(rice, pkg, 2, cost=3.0)]
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))], basket=basket,
                         pantry_used={rice.id: 500.0})
        pantry = Pantry()
        pantry.add(rice.id, 500.0)

        alloc = allocate_sources(plan, pantry, foods_by_id)[rice.id]
        assert alloc.from_pantry == pytest.approx(500.0)
        assert [(line.package_label, line.count) for line in alloc.to_buy] == [(pkg.label, 2)]
        assert alloc.to_buy[0].est_cost == pytest.approx(3.0)
        assert alloc.to_buy[0].package_grams == pytest.approx(pkg.grams)

    def test_float_dust_never_rounds_up_an_extra_package(self, rice, foods_by_id):
        pkg = rice.package_options[0]
        meal = make_meal([MealPortion(rice, pkg.grams * 2 + 0.002)])  # 3-dp dust
        basket = [make_basket_item(rice, pkg, 2, cost=3.0)]
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))], basket=basket)
        alloc = allocate_sources(plan, Pantry(), foods_by_id)[rice.id]
        assert [(line.package_label, line.count) for line in alloc.to_buy] == [(pkg.label, 2)]


class TestCovered:
    def test_covered_never_exceeds_requirement_despite_surplus_stock(self, rice, foods_by_id):
        meal = make_meal([MealPortion(rice, 300.0)])
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))])
        pantry = Pantry()
        pantry.add(rice.id, 10_000.0)  # huge surplus
        alloc = allocate_sources(plan, pantry, foods_by_id)[rice.id]
        assert alloc.covered == pytest.approx(300.0)
        assert alloc.covered <= alloc.meal_requirement

    def test_batch_dinner_and_leftover_lunch_counted_once(self, rice, foods_by_id):
        """The dinner's 2× draw already carries the leftover lunch's nutrition;
        Covered must equal exactly two servings' worth."""
        dinner = make_meal([MealPortion(rice, 300.0)], batch_id="b1")
        lunch = make_meal([MealPortion(rice, 300.0)], slot=MealSlot.LUNCH,
                          is_leftover=True, batch_id="b1")
        plan = make_plan(days=[
            DayPlan(day_index=0, meals=(dinner,)),
            DayPlan(day_index=1, meals=(lunch,)),
        ])
        pantry = Pantry()
        pantry.add(rice.id, 10_000.0)
        allocations = allocate_sources(plan, pantry, foods_by_id)
        assert allocations[rice.id].meal_requirement == pytest.approx(600.0)  # 2× once
        totals = covered_nutrients(allocations, foods_by_id)
        expected = rice.nutrients_per_purchased_100g().scaled(6.0)
        assert totals.calories_kcal == pytest.approx(expected.calories_kcal)

    def test_covered_nutrients_scale_with_coverage(self, rice, foods_by_id):
        meal = make_meal([MealPortion(rice, 300.0)])
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))])
        pantry = Pantry()
        pantry.add(rice.id, 150.0)  # half covered
        totals = covered_nutrients(allocate_sources(plan, pantry, foods_by_id), foods_by_id)
        expected = rice.nutrients_per_purchased_100g().scaled(1.5)
        assert totals.calories_kcal == pytest.approx(expected.calories_kcal)


class TestPackageFitting:
    def test_food_without_basket_line_uses_cheapest_catalog_package(self, rice, foods_by_id):
        meal = make_meal([MealPortion(rice, 100.0)])
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))])  # no basket at all
        alloc = allocate_sources(plan, Pantry(), foods_by_id)[rice.id]
        assert len(alloc.to_buy) == 1
        line = alloc.to_buy[0]
        cheapest = min(
            (p for p in rice.package_options if p.grams > 0),
            key=lambda p: p.seed_price / p.grams,
        )
        assert line.package_label == cheapest.label
        assert line.source == "seed_estimate"
        assert line.est_cost == pytest.approx(cheapest.seed_price)

    def test_gap_beyond_original_lines_tops_up_same_package(self, rice, foods_by_id):
        pkg = rice.package_options[0]
        meal = make_meal([MealPortion(rice, pkg.grams * 3.2)])  # needs 4 packages
        basket = [make_basket_item(rice, pkg, 2, cost=3.0)]  # plan only bought 2
        plan = make_plan(days=[DayPlan(day_index=0, meals=(meal,))], basket=basket)
        alloc = allocate_sources(plan, Pantry(), foods_by_id)[rice.id]
        assert [(line.package_label, line.count) for line in alloc.to_buy] == [(pkg.label, 4)]
        assert alloc.to_buy[0].est_cost == pytest.approx(4 * 1.5)  # unit cost 3.0/2


class TestHistorical:
    def test_ended_plans_are_historical_current_and_future_are_live(self):
        plan = make_plan(start=WHEN, horizon=7)  # ends Jul 13
        assert not is_historical(plan, today=WHEN)
        assert not is_historical(plan, today=WHEN + timedelta(days=6))
        assert is_historical(plan, today=WHEN + timedelta(days=7))
        future = make_plan(start=WHEN + timedelta(days=30), horizon=7)
        assert not is_historical(future, today=WHEN)
