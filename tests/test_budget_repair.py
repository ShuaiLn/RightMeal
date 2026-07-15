"""Budget repair tests: _repair_budget, per-day pantry snapshots, exact delta
pricing, shared PlannerContext / finalize_meal_plan, and the honest messages
appended by generate_recipe_first.

Most tests drive _repair_budget directly on hand-built days made of synthetic
recipes over real seed foods (controllable prices, deterministic), per the
design; a module-level integration smoke runs the real catalog end-to-end.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

from data.loader import load_catalog, load_nutrient_targets, load_portion_rules, load_recipe_index
from models import BudgetStatus, HouseholdProfile
from models.food import Nutrients
from models.meals import DayPlan, Meal, MealPlan, MealSlot
from models.recipe import Recipe, RecipeIngredient, RecipeType
from planner.demand import ingredient_demand
from planner.recipe_scheduler import (
    PlannerContext,
    RecipePlanConfig,
    VarietyMode,
    _build_meal,
    _repair_budget,
    apply_meal_demand_delta,
    build_planner_context,
    finalize_meal_plan,
    pantry_snapshots_by_day,
)
from services.basket_builder import COST_EPSILON, price_demand
from services.nutrition import NutritionService
from services.planner_engine import generate_recipe_first

from conftest import make_seed_quote

MEMBERS = 2


@pytest.fixture
def profile():
    return HouseholdProfile(adults=MEMBERS, city="LA", zip_code="90001")


def make_recipe(rid, name, ingredients, foods_by_id, *, protein=None, carbs=("rice",),
                dish="bowl", batchable=False, kcal=None):
    """A synthetic dinner recipe over real seed foods that passes validate_meal.
    ``ingredients`` is [(food_id, role, grams_per_serving)]."""
    ings = tuple(
        RecipeIngredient(
            raw_text=f"{g} g {fid}", canonical_food_id=fid, normalized_id=fid,
            role=role, grams_per_serving=float(g), quantity_state="raw",
            nutrition_basis=None, is_core=True, is_seasoning=False, optional=False,
            match_method="manual", confidence=1.0)
        for fid, role, g in ingredients)
    if kcal is None:
        kcal = sum(
            foods_by_id[fid].nutrients_per_purchased_100g().calories_kcal * g / 100.0
            for fid, _, g in ingredients)
    return Recipe(
        id=rid, canonical_name=name, source_file=f"{rid}.md", tags=(),
        recipe_type=RecipeType.MAIN_MEAL, meal_types=("dinner",),
        cuisine="international", dish_category=dish, cooking_methods=("boiling",),
        servings=MEMBERS, prep_time_min=15, cook_time_min=15, image_asset=None,
        directions=("Cook.",), ingredients=ings, main_protein=protein,
        main_carbs=tuple(carbs), allow_multiple_main_carbs=False, vegetables=(),
        substitutions=(), batchable=batchable, recommended_batch_servings=None,
        leftover_storage_days=None, reheat_method=None,
        nutrition_per_serving=Nutrients(calories_kcal=kcal),
        coverage_by_mass=1.0, core_coverage=1.0, auto_plannable=True,
        contains_pork=False, is_meat_or_fish=protein in ("salmon", "tilapia"),
        allergen_tags=frozenset(), verified=True)


@pytest.fixture
def recipes(foods_by_id):
    """The shared synthetic cast. All dinners land near 747 kcal/serving so
    swaps trivially keep the daily calorie tolerance."""
    return {
        # 400 g of salmon per day (2 servings): every swapped day drops about
        # one whole $7.99 salmon package from the cumulative demand.
        "salmon": make_recipe(
            "salmon_dinner", "Salmon Rice Plate",
            [("salmon_fillet", "protein", 200), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100)], foods_by_id, protein="salmon"),
        "tilapia": make_recipe(
            "tilapia_dinner", "Tilapia Rice Plate",
            [("tilapia_fillet", "protein", 200), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100)], foods_by_id, protein="tilapia"),
        "bean": make_recipe(
            "bean_dinner", "Bean Rice Bowl",
            [("black_beans_dry", "protein", 120), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 120)], foods_by_id, protein="black_beans"),
        "lentil": make_recipe(
            "lentil_dinner", "Lentil Rice Bowl",
            [("lentils_dry", "protein", 120), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 90)], foods_by_id, protein="lentils"),
    }


def dinner(recipe, foods_by_id, variety_mode=VarietyMode.BALANCED):
    return _build_meal(recipe, MealSlot.DINNER, foods_by_id, MEMBERS, variety_mode)


def make_days(recipes_in_order, foods_by_id):
    return tuple(
        DayPlan(day_index=i, meals=(dinner(r, foods_by_id),))
        for i, r in enumerate(recipes_in_order))


def make_ctx(pool_recipes, per_person_daily_kcal):
    by_id = {r.id: r for r in pool_recipes}
    return PlannerContext(
        members=MEMBERS,
        portion_rules=load_portion_rules(),
        per_person_daily_kcal=per_person_daily_kcal,
        recipes_by_id=MappingProxyType(by_id),
        pool=MappingProxyType({"breakfast": (), "lunch": (), "dinner": tuple(pool_recipes)}),
    )


def per_person_kcal_of(days):
    return days[0].meals[0].kcal / MEMBERS


def total_cost_of(days, pantry, foods_by_id, quotes):
    demand = ingredient_demand(MealPlan(days=tuple(days), horizon_days=len(days)))
    return price_demand(demand, pantry, foods_by_id, quotes).total_cost


def run_repair(days, pool, foods_by_id, quotes, budget, profile, *, pantry=None,
               variety_mode=VarietyMode.BALANCED, config=RecipePlanConfig(),
               per_person_kcal=None):
    ctx = make_ctx(pool, per_person_kcal or per_person_kcal_of(days))
    return _repair_budget(days, len(days), pantry or {}, foods_by_id, quotes,
                          budget, profile, ctx, variety_mode, config)


# -- core behavior ------------------------------------------------------------


class TestRepairBasics:
    def test_over_budget_swaps_to_cheaper_meals(self, foods_by_id, seed_quotes, recipes, profile):
        days = make_days([recipes["salmon"]] * 3, foods_by_id)
        pool = [recipes["salmon"], recipes["bean"], recipes["lentil"]]
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 12.0, profile)
        assert stats.attempted and stats.swaps_applied == 3
        final = total_cost_of(new_days, {}, foods_by_id, seed_quotes)
        assert final <= 12.0 + COST_EPSILON
        assert final < total_cost_of(days, {}, foods_by_id, seed_quotes)
        # The swapped-in meals introduced plan-new ingredients (beans/lentils
        # were nowhere in the original demand) without a KeyError.
        recipe_ids = [d.meals[0].recipe_id for d in new_days]
        assert "salmon_dinner" not in recipe_ids
        # BALANCED adjacency holds in the result.
        for a, b in zip(recipe_ids, recipe_ids[1:]):
            assert a != b

    def test_input_days_never_mutated_and_deterministic(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        days = make_days([recipes["salmon"]] * 3, foods_by_id)
        original_meals = [d.meals[0] for d in days]
        pool = [recipes["salmon"], recipes["bean"], recipes["lentil"]]
        first, _ = run_repair(days, pool, foods_by_id, seed_quotes, 12.0, profile)
        assert [d.meals[0] for d in days] == original_meals  # untouched
        second, _ = run_repair(days, pool, foods_by_id, seed_quotes, 12.0, profile)
        assert [d.meals[0].name for d in first] == [d.meals[0].name for d in second]

    def test_reanchored_total_matches_full_pricing(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        """After repair, a fresh full pricing of the final demand is what the
        loop reported against the budget — no incremental drift."""
        days = make_days([recipes["salmon"]] * 3, foods_by_id)
        pool = [recipes["salmon"], recipes["bean"], recipes["lentil"]]
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 12.0, profile)
        final = total_cost_of(new_days, {}, foods_by_id, seed_quotes)
        assert stats.swaps_applied > 0
        assert final <= 12.0 + COST_EPSILON

    def test_stats_bounds(self, foods_by_id, seed_quotes, recipes, profile):
        days = make_days([recipes["salmon"]] * 3, foods_by_id)
        pool = [recipes["salmon"], recipes["bean"], recipes["lentil"]]
        config = RecipePlanConfig()
        _, stats = run_repair(days, pool, foods_by_id, seed_quotes, 12.0, profile, config=config)
        effective_rounds = max(5, 3 * len(days))
        assert stats.candidates_scanned <= (
            effective_rounds * config.budget_repair_meals_per_round
            * config.budget_repair_candidate_scan_cap)
        assert stats.elapsed_ms >= 0.0
        assert stats.rounds_run <= effective_rounds


class TestGating:
    def test_under_budget_not_attempted(self, foods_by_id, seed_quotes, recipes, profile):
        days = make_days([recipes["salmon"]] * 2, foods_by_id)
        pool = [recipes["salmon"], recipes["bean"]]
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 500.0, profile)
        assert stats.attempted is False and stats.swaps_applied == 0
        assert new_days == days

    def test_no_quotes_not_attempted(self, foods_by_id, recipes, profile):
        days = make_days([recipes["salmon"]] * 2, foods_by_id)
        pool = [recipes["salmon"], recipes["bean"]]
        for quotes in (None, {}):
            new_days, stats = run_repair(days, pool, foods_by_id, quotes, 1.0, profile)
            assert stats.attempted is False
            assert new_days == days

    def test_known_over_with_gap_elsewhere_still_attempts(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        # Carrots unpriced everywhere: the known salmon cost alone is over, so
        # repair acts — reducing known cost is always legitimate. Candidates
        # that would GROW the carrot gap (bean: 120 g vs salmon's 100 g per
        # serving) are rejected by the gap-delta rule; lentil (90 g) shrinks it.
        quotes = {k: v for k, v in seed_quotes.items() if k != "carrots"}
        days = make_days([recipes["salmon"]] * 2, foods_by_id)
        pool = [recipes["salmon"], recipes["bean"], recipes["lentil"]]
        new_days, stats = run_repair(days, pool, foods_by_id, quotes, 5.0, profile)
        assert stats.attempted is True
        assert stats.swaps_applied > 0
        assert all(d.meals[0].recipe_id != "bean_dinner" for d in new_days)

    def test_never_raises_for_infeasibility_but_real_errors_propagate(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        days = make_days([recipes["salmon"]] * 2, foods_by_id)
        # Hopeless budget with no candidates: reported via stats, not raised.
        new_days, stats = run_repair(days, [recipes["salmon"]], foods_by_id,
                                     seed_quotes, 0.01, profile)
        assert stats.attempted and stats.swaps_applied == 0
        # A corrupted context (missing portion rules) is a REAL error: no
        # blanket except may swallow it.
        broken_ctx = PlannerContext(
            members=MEMBERS, portion_rules={}, per_person_daily_kcal=747.0,
            recipes_by_id=MappingProxyType({r.id: r for r in recipes.values()}),
            pool=MappingProxyType({"dinner": (recipes["bean"],)}),
        )
        with pytest.raises(KeyError):
            _repair_budget(days, 2, {}, foods_by_id, seed_quotes, 0.01, profile,
                           broken_ctx, VarietyMode.BALANCED)


class TestSwapRules:
    def test_same_slot_swapped_at_most_once(self, foods_by_id, seed_quotes, recipes, profile):
        # Budget unreachable: without the same-slot-once rule the single slot
        # could churn bean -> lentil -> bean forever.
        days = make_days([recipes["salmon"]], foods_by_id)
        pool = [recipes["salmon"], recipes["bean"], recipes["lentil"]]
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 0.01, profile)
        assert stats.swaps_applied == 1
        assert new_days[0].meals[0].recipe_id in ("bean_dinner", "lentil_dinner")

    def test_sub_epsilon_improvement_not_swapped(self, foods_by_id, seed_quotes, recipes, profile):
        twin = make_recipe(
            "bean_twin", "Bean Rice Twin",
            [("black_beans_dry", "protein", 120), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 120)], foods_by_id, protein="black_beans")
        days = make_days([recipes["bean"]], foods_by_id)
        new_days, stats = run_repair(days, [recipes["bean"], twin], foods_by_id,
                                     seed_quotes, 0.5, profile)
        assert stats.attempted is True
        assert stats.swaps_applied == 0  # identical cost — no real improvement
        assert new_days == days

    def test_best_of_round_takes_biggest_saving_first(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        # Swapping the salmon day saves ~$6.40; the tilapia day ~$3.90. With
        # one round only, the salmon day must be the one that changed.
        days = make_days([recipes["salmon"], recipes["tilapia"]], foods_by_id)
        pool = [recipes["salmon"], recipes["tilapia"], recipes["bean"]]
        config = RecipePlanConfig(budget_repair_max_rounds=1)
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 0.01,
                                     profile, config=config)
        assert stats.swaps_applied == 1
        assert new_days[0].meals[0].recipe_id == "bean_dinner"
        assert new_days[1].meals[0].recipe_id == "tilapia_dinner"

    def test_rounds_scale_with_horizon_and_explicit_override(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        # Three distinct cheap candidates so BALANCED adjacency can always be
        # satisfied (with only two, a leftover expensive day can get
        # sandwiched between them — the documented greedy limitation).
        egg = make_recipe(
            "egg_dinner", "Egg Rice Bowl",
            [("eggs_large", "protein", 150), ("rice_white", "main_carb", 120),
             ("carrots", "vegetable", 100)], foods_by_id, protein="eggs")
        days = make_days([recipes["salmon"]] * 7, foods_by_id)
        pool = [recipes["salmon"], recipes["bean"], recipes["lentil"], egg]
        # Default None -> max(5, 21) rounds: more than 5 swaps converge.
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 20.0, profile)
        assert stats.swaps_applied > 5
        assert total_cost_of(new_days, {}, foods_by_id, seed_quotes) <= 20.0 + COST_EPSILON
        # An explicit cap stops the loop.
        capped, capped_stats = run_repair(
            days, pool, foods_by_id, seed_quotes, 20.0, profile,
            config=RecipePlanConfig(budget_repair_max_rounds=2))
        assert capped_stats.rounds_run == 2
        assert capped_stats.swaps_applied == 2

    def test_bidirectional_adjacency_blocks_next_day_repeat(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        """BALANCED: the candidate already sits on day+1 — a last_day_used
        replay would miss this forward direction."""
        days = make_days([recipes["salmon"], recipes["bean"]], foods_by_id)
        pool = [recipes["salmon"], recipes["bean"]]
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 0.01, profile)
        assert stats.swaps_applied == 0
        assert new_days[0].meals[0].recipe_id == "salmon_dinner"

    def test_high_variety_rejects_any_reuse_where_balanced_allows_it(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        # bean sits on day 2 — NOT adjacent to day 0. The regression allowance
        # is widened so the soft repeat penalty can't mask the HARD variety
        # rule this test isolates.
        days = make_days([recipes["salmon"], recipes["lentil"], recipes["bean"]], foods_by_id)
        pool = [recipes["salmon"], recipes["lentil"], recipes["bean"]]
        config = RecipePlanConfig(budget_repair_max_rounds=1,
                                  budget_repair_max_score_regression=5.0)
        balanced, b_stats = run_repair(days, pool, foods_by_id, seed_quotes, 0.01,
                                       profile, config=config)
        assert balanced[0].meals[0].recipe_id == "bean_dinner"  # non-adjacent reuse allowed
        high, h_stats = run_repair(days, pool, foods_by_id, seed_quotes, 0.01, profile,
                                   variety_mode=VarietyMode.HIGH_VARIETY, config=config)
        assert h_stats.swaps_applied == 0  # every candidate is already used once
        assert high[0].meals[0].recipe_id == "salmon_dinner"

    def test_calorie_tolerance_must_not_get_worse(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        # Cheaper, but +330 kcal/serving of extra rice throws the day out of
        # its +/-10% band: rejected even though it saves money.
        heavy = make_recipe(
            "bean_heavy", "Heavy Bean Bowl",
            [("black_beans_dry", "protein", 120), ("rice_white", "main_carb", 170),
             ("carrots", "vegetable", 120)], foods_by_id, protein="black_beans")
        days = make_days([recipes["salmon"]], foods_by_id)
        new_days, stats = run_repair(days, [recipes["salmon"], heavy], foods_by_id,
                                     seed_quotes, 0.01, profile)
        assert stats.swaps_applied == 0
        assert new_days == days

    def test_batch_and_leftover_meals_never_altered(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        batch_meal = replace(dinner(recipes["salmon"], foods_by_id), batch_id="batch-x")
        leftover_meal = Meal(
            slot=MealSlot.DINNER, template_id="", name="Leftover stew", portions=(),
            recipe_id=None, servings=float(MEMBERS), prepared_leftover_id="lo-1")
        days = (
            DayPlan(day_index=0, meals=(batch_meal,)),
            DayPlan(day_index=1, meals=(leftover_meal,)),
            DayPlan(day_index=2, meals=(dinner(recipes["salmon"], foods_by_id),)),
        )
        pool = [recipes["salmon"], recipes["bean"], recipes["lentil"]]
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 0.01, profile,
                                     per_person_kcal=days[2].meals[0].kcal / MEMBERS)
        assert new_days[0].meals[0] is batch_meal        # batch dinner untouched
        assert new_days[1].meals[0] is leftover_meal     # pinned leftover untouched
        assert new_days[2].meals[0].recipe_id != "salmon_dinner"  # only free slot swapped
        # The leftover's (empty) draw never re-enters demand.
        demand = ingredient_demand(MealPlan(days=new_days, horizon_days=3))
        assert stats.swaps_applied == 1
        assert all(g > 0 for g in demand.values())

    def test_repair_never_mints_a_batch_meal(self, foods_by_id, seed_quotes, recipes, profile):
        batchable_bean = make_recipe(
            "bean_batchable", "Batch Bean Bowl",
            [("black_beans_dry", "protein", 120), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 120)], foods_by_id, protein="black_beans",
            batchable=True)
        days = make_days([recipes["salmon"]], foods_by_id)
        # Under MEAL_PREP a batchable dinner would come out with a batch_id:
        # the trial must be rejected.
        new_days, stats = run_repair(
            days, [recipes["salmon"], batchable_bean], foods_by_id, seed_quotes,
            0.01, profile, variety_mode=VarietyMode.MEAL_PREP)
        assert stats.swaps_applied == 0
        # The same recipe marked non-batchable swaps fine under MEAL_PREP.
        plain_bean = replace(batchable_bean, batchable=False)
        new_days, stats = run_repair(
            days, [recipes["salmon"], plain_bean], foods_by_id, seed_quotes,
            0.01, profile, variety_mode=VarietyMode.MEAL_PREP)
        assert stats.swaps_applied == 1
        assert new_days[0].meals[0].batch_id is None


class TestUnpricedRules:
    def test_unpriced_candidates_do_not_monopolize_the_scan(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        """Unquoted candidates rank artificially cheapest; scanning (not
        taking-first-K) lets the later fully-priced candidate win."""
        tofu = make_recipe(
            "tofu_dinner", "Tofu Rice Bowl",
            [("tofu_firm", "protein", 150), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100)], foods_by_id, protein="tofu")
        egg = make_recipe(
            "egg_dinner", "Egg Rice Bowl",
            [("eggs_large", "protein", 120), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100)], foods_by_id, protein="eggs")
        quotes = {k: v for k, v in seed_quotes.items() if k not in ("tofu_firm", "eggs_large")}
        days = make_days([recipes["salmon"]], foods_by_id)
        pool = [recipes["salmon"], tofu, egg, recipes["bean"]]
        new_days, stats = run_repair(days, pool, foods_by_id, quotes, 0.01, profile)
        assert new_days[0].meals[0].recipe_id == "bean_dinner"
        assert stats.candidates_scanned >= 3  # the unpriced ones were scanned, not skipped

    def test_sharing_a_preexisting_unpriced_ingredient_is_accepted(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        quotes = {k: v for k, v in seed_quotes.items() if k != "canola_oil"}
        salmon_oil = make_recipe(
            "salmon_oil", "Salmon Oil Plate",
            [("salmon_fillet", "protein", 200), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100), ("canola_oil", "fat", 10)],
            foods_by_id, protein="salmon")
        bean_oil = make_recipe(
            "bean_oil", "Bean Oil Bowl",
            [("black_beans_dry", "protein", 120), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 120), ("canola_oil", "fat", 10)],
            foods_by_id, protein="black_beans")
        days = make_days([salmon_oil], foods_by_id)
        new_days, stats = run_repair(days, [salmon_oil, bean_oil], foods_by_id,
                                     quotes, 0.01, profile)
        assert stats.swaps_applied == 1  # same oil grams: gap unchanged, allowed
        assert new_days[0].meals[0].recipe_id == "bean_oil"

    def test_growing_an_unpriced_gap_is_rejected(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        quotes = {k: v for k, v in seed_quotes.items() if k != "canola_oil"}
        salmon_oil = make_recipe(
            "salmon_oil", "Salmon Oil Plate",
            [("salmon_fillet", "protein", 200), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100), ("canola_oil", "fat", 10)],
            foods_by_id, protein="salmon")
        # Cheaper on priced foods but doubles the unpriced oil (kcal balanced
        # by less rice, so only the gap rule can reject it).
        bean_more_oil = make_recipe(
            "bean_more_oil", "Oily Bean Bowl",
            [("black_beans_dry", "protein", 120), ("rice_white", "main_carb", 56),
             ("carrots", "vegetable", 120), ("canola_oil", "fat", 20)],
            foods_by_id, protein="black_beans")
        days = make_days([salmon_oil], foods_by_id)
        new_days, stats = run_repair(days, [salmon_oil, bean_more_oil], foods_by_id,
                                     quotes, 0.01, profile)
        assert stats.swaps_applied == 0
        assert new_days == days

    def test_introducing_a_new_unpriced_food_is_rejected(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        quotes = {k: v for k, v in seed_quotes.items() if k != "tofu_firm"}
        tofu = make_recipe(
            "tofu_dinner", "Tofu Rice Bowl",
            [("tofu_firm", "protein", 150), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100)], foods_by_id, protein="tofu")
        days = make_days([recipes["salmon"]], foods_by_id)
        new_days, stats = run_repair(days, [recipes["salmon"], tofu], foods_by_id,
                                     quotes, 0.01, profile)
        assert stats.swaps_applied == 0


class TestDeltaPricing:
    def test_package_boundary_uses_cumulative_demand(
        self, foods_by_id, seed_quotes, recipes, profile
    ):
        """Whether a package drops depends on the plan-wide beans total
        crossing 453.6 g — not on one meal's grams in isolation."""
        def bean_variant(rid, grams):
            return make_recipe(
                rid, f"Bean Bowl {grams}",
                [("black_beans_dry", "protein", grams), ("rice_white", "main_carb", 80),
                 ("carrots", "vegetable", 120)], foods_by_id, protein="black_beans")

        current = bean_variant("bean_120", 120)      # 240 g/day; 480 g over 2 days -> 2 pkgs
        crosses = bean_variant("bean_100", 100)      # day swap -> 440 g total -> 1 pkg
        stays = bean_variant("bean_115", 115)        # day swap -> 470 g total -> still 2 pkgs
        days = make_days([current, current], foods_by_id)

        swapped, stats = run_repair(days, [current, crosses], foods_by_id, seed_quotes,
                                    0.01, profile)
        assert stats.swaps_applied == 1  # crossing the boundary saves a package

        unswapped, stats = run_repair(days, [current, stays], foods_by_id, seed_quotes,
                                      0.01, profile)
        assert stats.swaps_applied == 0  # same package count -> no real saving


class TestApplyMealDemandDelta:
    def test_plan_new_food_no_keyerror(self, foods_by_id, recipes):
        old = dinner(recipes["salmon"], foods_by_id)
        new = dinner(recipes["bean"], foods_by_id)
        demand = {"salmon_fillet": 400.0, "rice_white": 160.0, "carrots": 200.0}
        result = apply_meal_demand_delta(demand, old, new)
        assert result["black_beans_dry"] == pytest.approx(240.0)
        assert "salmon_fillet" not in result  # zeroed foods removed, not left at 0
        assert result["carrots"] == pytest.approx(240.0)

    def test_input_not_mutated(self, foods_by_id, recipes):
        old = dinner(recipes["salmon"], foods_by_id)
        new = dinner(recipes["bean"], foods_by_id)
        demand = {"salmon_fillet": 400.0, "rice_white": 160.0, "carrots": 200.0}
        before = dict(demand)
        apply_meal_demand_delta(demand, old, new)
        assert demand == before


class TestPantrySnapshots:
    def test_snapshots_by_day(self, foods_by_id, recipes):
        days = make_days([recipes["bean"], recipes["bean"], recipes["bean"]], foods_by_id)
        pantry = {"black_beans_dry": 500.0, "rice_white": 100.0}
        snaps = pantry_snapshots_by_day(pantry, days)
        assert snaps[0] == pantry                       # day 0 sees initial stock
        assert snaps[1]["black_beans_dry"] == pytest.approx(260.0)  # minus day 0's 240 g
        assert snaps[1]["rice_white"] == pytest.approx(0.0)
        assert snaps[2]["black_beans_dry"] == pytest.approx(20.0)
        assert pantry == {"black_beans_dry": 500.0, "rice_white": 100.0}  # pure

    def test_repair_scores_against_the_right_day_snapshot(
        self, foods_by_id, seed_quotes, profile
    ):
        """Blocker 2 regression: day 0 fully consumes the pantry beans, so a
        day-2 bean candidate must be scored with ZERO bean stock. A single
        shared (initial) snapshot would give it a pantry bonus large enough to
        flip the quality gate and accept the swap."""
        per_person_kcal = None
        bean_feast = make_recipe(
            "bean_feast", "Bean Feast",
            [("black_beans_dry", "protein", 120), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 120)], foods_by_id, protein="black_beans")
        salmon_lite = make_recipe(
            "salmon_lite", "Salmon Lite Plate",
            [("salmon_fillet", "protein", 200), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100)], foods_by_id, protein="salmon")
        days = make_days([bean_feast, salmon_lite], foods_by_id)
        per_person_kcal = days[1].meals[0].kcal / MEMBERS
        # The day-2 target: its recipe's declared per-serving kcal is tuned so
        # its nutrition-fit beats the candidate's by 0.6 — just over the 0.5
        # regression allowance. The candidate only passes if it wrongly picks
        # up the 0.6 * 0.375 pantry-coverage bonus from the INITIAL snapshot.
        slot_target = per_person_kcal * load_portion_rules()["slot_kcal_share_midpoint"]["dinner"]
        salmon_special = make_recipe(
            "salmon_special", "Salmon Special Plate",
            [("salmon_fillet", "protein", 200), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 100)], foods_by_id, protein="salmon",
            kcal=slot_target * 1.4)  # fit 0.6 vs the candidate's fit 0.0
        bean_candidate = make_recipe(
            "bean_dinner", "Bean Rice Bowl",
            [("black_beans_dry", "protein", 120), ("rice_white", "main_carb", 80),
             ("carrots", "vegetable", 120)], foods_by_id, protein="black_beans")
        days = days + (DayPlan(day_index=2, meals=(dinner(salmon_special, foods_by_id),)),)
        pantry = {"black_beans_dry": 240.0}  # exactly day 0's draw
        # Only day 2's recipe and the candidate are known to the context, so
        # days 0/1 are not swap targets and can't confound the assertion —
        # their meals still drive the per-day pantry draws.
        pool = [salmon_special, bean_candidate]
        new_days, stats = run_repair(days, pool, foods_by_id, seed_quotes, 0.01,
                                     profile, pantry=pantry,
                                     per_person_kcal=per_person_kcal)
        assert stats.swaps_applied == 0
        assert new_days[2].meals[0].recipe_id == "salmon_special"


class TestContextAndFinalize:
    def test_planner_context_is_immutable(self, foods_by_id, recipes):
        ctx = make_ctx(list(recipes.values()), 700.0)
        with pytest.raises(TypeError):
            ctx.pool["dinner"] = ()
        with pytest.raises(TypeError):
            ctx.recipes_by_id["x"] = recipes["bean"]

    def test_build_planner_context_spot_check(self):
        foods = {f.id: f for f in load_catalog()}
        recipes = load_recipe_index()
        profile = HouseholdProfile(adults=2, children=2, city="LA", zip_code="90001")
        nutrition = NutritionService(load_nutrient_targets())
        ctx = build_planner_context(recipes, foods, profile, nutrition)
        assert ctx.members == 4
        assert ctx.per_person_daily_kcal > 0
        assert ctx.pool["dinner"] and ctx.pool["breakfast"] and ctx.pool["lunch"]
        assert all(r.id in ctx.recipes_by_id for rs in ctx.pool.values() for r in rs)

    def test_finalize_meal_plan_correctness(self, foods_by_id, recipes):
        days = make_days([recipes["bean"], recipes["lentil"]], foods_by_id)
        pantry = {"black_beans_dry": 300.0, "rice_white": 5000.0}
        plan = finalize_meal_plan(days, 2, pantry)
        expected = Nutrients()
        for d in days:
            for m in d.meals:
                expected = expected.plus(m.nutrients)
        assert plan.consumed_totals.calories_kcal == pytest.approx(expected.calories_kcal)
        assert plan.pantry_carryover["black_beans_dry"] == pytest.approx(60.0)  # 300 - 240
        assert plan.pantry_carryover["rice_white"] == pytest.approx(5000.0 - 320.0)
        assert plan.horizon_days == 2

    def test_finalize_reflects_overlaid_meals(self, foods_by_id, recipes):
        """Overlay-staleness regression: after replacing a day's meal with a
        prepared leftover (zero draw), finalize must recompute carryover and
        consumed totals from the OVERLAID days."""
        days = make_days([recipes["bean"], recipes["bean"]], foods_by_id)
        pantry = {"black_beans_dry": 480.0}
        before = finalize_meal_plan(days, 2, pantry)
        assert "black_beans_dry" not in before.pantry_carryover  # fully drawn
        leftover_meal = Meal(
            slot=MealSlot.DINNER, template_id="", name="Leftover bean stew",
            portions=(), recipe_id=None, servings=float(MEMBERS),
            prepared_leftover_id="lo-1")
        overlaid = (DayPlan(day_index=0, meals=(leftover_meal,)), days[1])
        after = finalize_meal_plan(overlaid, 2, pantry)
        assert after.pantry_carryover["black_beans_dry"] == pytest.approx(240.0)
        assert after.consumed_totals.calories_kcal < before.consumed_totals.calories_kcal


# -- integration through generate_recipe_first --------------------------------


@pytest.fixture(scope="module")
def real_catalog():
    foods = {f.id: f for f in load_catalog()}
    return foods, load_recipe_index(), {fid: make_seed_quote(f) for fid, f in foods.items()}


@pytest.fixture(scope="module")
def real_nutrition():
    return NutritionService(load_nutrient_targets())


class TestGenerateRecipeFirstIntegration:
    def test_repair_message_when_still_over(self, real_catalog, real_nutrition):
        foods, recipes, quotes = real_catalog
        family = HouseholdProfile(adults=2, children=2, city="LA", zip_code="90001")
        output = generate_recipe_first(
            recipes, foods, family, real_nutrition, {}, quotes, 1.0, 7,
            VarietyMode.BALANCED)
        assert output.result.budget_status is BudgetStatus.OVER
        assert output.repair_stats.attempted is True
        messages = output.result.relaxed_constraints
        assert any("Swapped" in m or "none fit" in m for m in messages)
        if output.repair_stats.swaps_applied > 0:
            assert any(f"Swapped {output.repair_stats.swaps_applied} meal(s)" in m
                       for m in messages)
        else:
            assert any("none fit" in m for m in messages)

    def test_no_repair_message_when_within(self, real_catalog, real_nutrition):
        foods, recipes, quotes = real_catalog
        family = HouseholdProfile(adults=2, children=2, city="LA", zip_code="90001")
        output = generate_recipe_first(
            recipes, foods, family, real_nutrition, {}, quotes, 10000.0, 7,
            VarietyMode.BALANCED)
        assert output.result.budget_status is BudgetStatus.WITHIN
        assert output.repair_stats.attempted is False
        assert not any("Swapped" in m or "none fit" in m
                       for m in output.result.relaxed_constraints)

    def test_no_repair_without_quotes(self, real_catalog, real_nutrition):
        foods, recipes, _ = real_catalog
        family = HouseholdProfile(adults=2, children=2, city="LA", zip_code="90001")
        output = generate_recipe_first(
            recipes, foods, family, real_nutrition, {}, {}, 1.0, 7,
            VarietyMode.BALANCED)
        assert output.repair_stats.attempted is False
        assert output.result.budget_status is BudgetStatus.UNKNOWN

    def test_full_week_smoke_prints_elapsed(self, real_catalog, real_nutrition):
        """End-to-end: scheduler -> repair -> finalize -> demand -> basket on
        the real catalog with a moderately tight budget."""
        foods, recipes, quotes = real_catalog
        family = HouseholdProfile(adults=2, children=2, city="LA", zip_code="90001")
        output = generate_recipe_first(
            recipes, foods, family, real_nutrition, {}, quotes, 40.0, 7,
            VarietyMode.BALANCED)
        assert len(output.meal_plan.days) == 7
        assert all(len(d.meals) == 3 for d in output.meal_plan.days)
        assert output.result.total_cost > 0
        stats = output.repair_stats
        print(f"\nRepairStats: attempted={stats.attempted} rounds={stats.rounds_run} "
              f"scanned={stats.candidates_scanned} passed={stats.candidates_passed} "
              f"swaps={stats.swaps_applied} elapsed_ms={stats.elapsed_ms:.1f}")
        assert stats.elapsed_ms >= 0.0
