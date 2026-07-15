"""Recipe service: strict validation, versioned cache keys, store round-trip."""

import json

import httpx
import pytest
from conftest import openai_client

from models import HouseholdProfile, Meal, MealPortion, MealSlot
from services.recipe_service import (
    RECIPE_SCHEMA_VERSION,
    RecipeService,
    build_recipe_request,
    get_recipe_service,
    profile_restrictions,
    recipe_cache_key,
)
from services.recipe_store import RecipeStore

STEPS = [
    "Rinse the rice and cook it in water until tender.",
    "Season the chicken with optional salt and pepper.",
    "Pan-fry the chicken in a little oil until cooked through.",
    "Serve the chicken over the rice.",
]


@pytest.fixture
def meal(foods_by_id) -> Meal:
    rice = foods_by_id["rice_white"]
    chicken = foods_by_id["chicken_breast"]
    return Meal(
        slot=MealSlot.DINNER,
        template_id="t",
        name="Chicken and rice",
        portions=(MealPortion(rice, 200.0, cooked_grams=480.0), MealPortion(chicken, 300.0)),
    )


def service(payload=None, status=200, catalog=()) -> RecipeService:
    return RecipeService("sk-test", openai_client(payload, status), catalog=tuple(catalog))


class TestGenerate:
    async def test_happy_path(self, meal):
        request = build_recipe_request(meal, HouseholdProfile(adults=2))
        steps = await service({"steps": STEPS}).generate(request)
        assert steps == STEPS

    async def test_allowlisted_seasonings_pass_despite_catalog(self, meal, foods):
        request = build_recipe_request(meal, None)
        steps = await service({"steps": STEPS}, catalog=foods).generate(request)
        assert steps == STEPS  # "salt and pepper" never trips the foreign check

    async def test_foreign_catalog_food_rejects_everything(self, meal, foods):
        meal_ids = {p.food.id for p in meal.portions}
        foreign = next(
            f for f in foods if f.id not in meal_ids and len(f.name.split()) >= 2
        )
        payload = {"steps": [*STEPS[:3], f"Fold in the {foreign.name} at the end."]}
        request = build_recipe_request(meal, None)
        assert await service(payload, catalog=foods).generate(request) is None

    async def test_url_rejects_everything(self, meal):
        payload = {"steps": [*STEPS[:3], "See https://example.com for details."]}
        assert await service(payload).generate(build_recipe_request(meal, None)) is None

    async def test_empty_or_oversized_steps_reject(self, meal):
        request = build_recipe_request(meal, None)
        assert await service({"steps": [*STEPS[:3], "  "]}).generate(request) is None
        assert await service({"steps": [*STEPS[:3], "x" * 500]}).generate(request) is None
        assert await service({"steps": []}).generate(request) is None
        assert await service({"steps": "boil it"}).generate(request) is None

    async def test_http_error_and_malformed_json_return_none(self, meal):
        request = build_recipe_request(meal, None)
        assert await service(status=500).generate(request) is None
        assert await service("{not json").generate(request) is None


class TestRequestShape:
    async def test_request_carries_facts_and_strict_schema(self, meal):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps({"steps": STEPS})}}]}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        profile = HouseholdProfile(adults=2, children=1, vegetarian=True, allergies=["peanuts"])
        request = build_recipe_request(meal, profile)
        assert await RecipeService("sk-test", client).generate(request) == STEPS

        assert captured["response_format"]["json_schema"]["strict"] is True
        sent = json.loads(captured["messages"][1]["content"])
        assert sent["serves"] == 3
        assert {i["food_name"] for i in sent["ingredients"]} == {
            p.food.name for p in meal.portions
        }
        rice_entry = next(i for i in sent["ingredients"] if i["raw_grams"] == 200.0)
        assert rice_entry["cooked_grams"] == 480.0
        assert "vegetarian" in sent["dietary_restrictions"]
        assert "allergy: peanuts" in sent["dietary_restrictions"]


class TestCacheKey:
    def test_stable_for_identical_requests(self, meal):
        profile = HouseholdProfile(adults=2)
        assert recipe_cache_key(build_recipe_request(meal, profile)) == recipe_cache_key(
            build_recipe_request(meal, profile)
        )

    def test_changes_with_restrictions(self, meal):
        plain = recipe_cache_key(build_recipe_request(meal, HouseholdProfile(adults=2)))
        vegetarian = recipe_cache_key(
            build_recipe_request(meal, HouseholdProfile(adults=2, vegetarian=True))
        )
        allergy = recipe_cache_key(
            build_recipe_request(meal, HouseholdProfile(adults=2, allergies=["milk"]))
        )
        assert len({plain, vegetarian, allergy}) == 3

    def test_changes_with_servings_and_version(self, meal, monkeypatch):
        one = recipe_cache_key(build_recipe_request(meal, HouseholdProfile(adults=1)))
        two = recipe_cache_key(build_recipe_request(meal, HouseholdProfile(adults=2)))
        assert one != two
        monkeypatch.setattr(
            "services.recipe_service.RECIPE_SCHEMA_VERSION", RECIPE_SCHEMA_VERSION + 1
        )
        bumped = recipe_cache_key(build_recipe_request(meal, HouseholdProfile(adults=1)))
        assert bumped != one

    def test_restrictions_normalize_from_profile(self):
        profile = HouseholdProfile(
            adults=1, vegetarian=True, no_pork=True, lactose_free=True,
            allergies=["shellfish", "peanuts"],
        )
        assert profile_restrictions(profile) == (
            "vegetarian", "no pork", "lactose-free",
            "allergy: peanuts", "allergy: shellfish",
        )
        assert profile_restrictions(None) == ()


class TestFactory:
    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert get_recipe_service(None, openai_client({})) is None

    def test_profile_key_wins(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        profile = HouseholdProfile(adults=1, api_keys={"openai_api_key": "sk-test"})
        assert get_recipe_service(profile, openai_client({})) is not None


class TestRecipeStore:
    def test_round_trip(self, tmp_path):
        store = RecipeStore(tmp_path)
        store.save({"key1": STEPS})
        assert RecipeStore(tmp_path).load() == {"key1": STEPS}

    def test_missing_and_corrupt_files_load_empty(self, tmp_path):
        assert RecipeStore(tmp_path).load() == {}
        (tmp_path / "recipes.json").write_text("{oops", encoding="utf-8")
        assert RecipeStore(tmp_path).load() == {}

    def test_empty_step_lists_are_dropped(self, tmp_path):
        (tmp_path / "recipes.json").write_text(
            json.dumps({"version": 1, "recipes": {"a": [], "b": ["step"]}}),
            encoding="utf-8",
        )
        assert RecipeStore(tmp_path).load() == {"b": ["step"]}
