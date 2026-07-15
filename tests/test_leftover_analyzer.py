"""Leftover analyzer: strict validation, failure -> None (manual fallback)."""

import json

import pytest
from conftest import openai_client

from models import HouseholdProfile, Meal, MealPortion, MealSlot
from services.leftover_analyzer import (
    LeftoverAnalyzer,
    get_leftover_analyzer,
    serialize_meal,
)


@pytest.fixture
def meal(foods_by_id) -> Meal:
    rice = foods_by_id["rice_white"]
    chicken = foods_by_id["chicken_breast"]
    return Meal(
        slot=MealSlot.DINNER,
        template_id="t",
        name="Chicken fried rice",
        portions=(MealPortion(rice, 200.0), MealPortion(chicken, 300.0)),
        batch_id="b1",
    )


def analyzer(payload=None, status=200, raise_timeout=False) -> LeftoverAnalyzer:
    return LeftoverAnalyzer("sk-test", openai_client(payload, status, raise_timeout))


class TestAnalyze:
    async def test_happy_path_with_components(self, meal):
        payload = {
            "leftover_fraction": 0.33,
            "components": [
                {"food_id": "rice_white", "remaining_fraction": 0.5},
                {"food_id": "chicken_breast", "remaining_fraction": 0.0},
            ],
        }
        estimate = await analyzer(payload).analyze(meal, "米饭剩一半鸡肉吃完了")
        assert estimate is not None
        assert estimate.overall_fraction == pytest.approx(0.33)
        assert estimate.components == {"rice_white": 0.5, "chicken_breast": 0.0}

    async def test_empty_components_means_uniform(self, meal):
        payload = {"leftover_fraction": 0.25, "components": []}
        estimate = await analyzer(payload).analyze(meal, "left about a quarter")
        assert estimate.overall_fraction == pytest.approx(0.25)
        assert estimate.components == {}

    async def test_values_are_clamped(self, meal):
        payload = {
            "leftover_fraction": 1.7,
            "components": [{"food_id": "rice_white", "remaining_fraction": -0.2}],
        }
        estimate = await analyzer(payload).analyze(meal, "note")
        assert estimate.overall_fraction == 1.0
        assert estimate.components == {"rice_white": 0.0}

    async def test_null_fraction_returns_none(self, meal):
        payload = {"leftover_fraction": None, "components": []}
        assert await analyzer(payload).analyze(meal, "nice weather") is None

    async def test_boolean_fraction_returns_none(self, meal):
        payload = {"leftover_fraction": True, "components": []}
        assert await analyzer(payload).analyze(meal, "note") is None

    async def test_hallucinated_food_id_rejects_everything(self, meal):
        payload = {
            "leftover_fraction": 0.5,
            "components": [{"food_id": "caviar", "remaining_fraction": 0.5}],
        }
        assert await analyzer(payload).analyze(meal, "note") is None

    async def test_duplicate_food_id_rejects_everything(self, meal):
        payload = {
            "leftover_fraction": 0.5,
            "components": [
                {"food_id": "rice_white", "remaining_fraction": 0.5},
                {"food_id": "rice_white", "remaining_fraction": 0.9},
            ],
        }
        assert await analyzer(payload).analyze(meal, "note") is None

    async def test_non_numeric_component_returns_none(self, meal):
        payload = {
            "leftover_fraction": 0.5,
            "components": [{"food_id": "rice_white", "remaining_fraction": "half"}],
        }
        assert await analyzer(payload).analyze(meal, "note") is None

    async def test_http_error_returns_none(self, meal):
        assert await analyzer(status=500).analyze(meal, "note") is None

    async def test_timeout_returns_none(self, meal):
        assert await analyzer(raise_timeout=True).analyze(meal, "note") is None

    async def test_malformed_json_returns_none(self, meal):
        assert await analyzer("{not json").analyze(meal, "note") is None

    async def test_missing_key_returns_none(self, meal):
        assert await analyzer({"components": []}).analyze(meal, "note") is None


class TestRequestShape:
    async def test_request_carries_meal_facts_and_strict_schema(self, meal):
        import httpx

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            content = json.dumps({"leftover_fraction": 0.5, "components": []})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await LeftoverAnalyzer("sk-test", client).analyze(meal, "剩了一半")
        assert captured["response_format"]["json_schema"]["strict"] is True
        assert "currently served" in captured["messages"][0]["content"].lower()
        user_payload = json.loads(captured["messages"][1]["content"])
        assert user_payload["meal_name"] == "Chicken fried rice"
        assert user_payload["is_batch_cooked"] is True
        assert user_payload["user_note"] == "剩了一半"
        assert {p["food_id"] for p in user_payload["portions"]} == {
            "rice_white", "chicken_breast",
        }

    def test_serialize_meal_aggregates_same_food(self, foods_by_id):
        rice = foods_by_id["rice_white"]
        meal = Meal(
            slot=MealSlot.DINNER,
            template_id="t",
            name="Rice",
            portions=(MealPortion(rice, 120.0), MealPortion(rice, 80.0)),
        )
        payload = serialize_meal(meal, "note")
        assert payload["portions"] == [
            {"food_id": "rice_white", "food_name": rice.name, "grams": 200.0}
        ]


class TestFactory:
    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = openai_client({})
        assert get_leftover_analyzer(None, client) is None
        assert get_leftover_analyzer(HouseholdProfile(adults=1), client) is None

    def test_profile_key_wins(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        profile = HouseholdProfile(adults=1, api_keys={"openai_api_key": "sk-test"})
        assert get_leftover_analyzer(profile, openai_client({})) is not None

    def test_env_key_works(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        assert get_leftover_analyzer(None, openai_client({})) is not None
