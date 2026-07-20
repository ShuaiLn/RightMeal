"""Explanation layer tests: local templates, OpenAI validation, fallbacks."""

import pytest

from conftest import openai_client, result_from_demand
from models import BudgetStatus, HouseholdProfile
from services.explanation import (
    LocalExplanationService,
    OpenAIExplanationService,
    get_explanation_service,
)

@pytest.fixture
def feasible_result(foods_by_id, seed_quotes, nutrition):
    profile = HouseholdProfile(adults=1, city="Los Angeles", zip_code="90001")
    demand = {"rice_white": 1400.0, "eggs_large": 700.0, "milk_whole": 2000.0,
              "bananas": 1000.0, "chicken_breast": 900.0, "carrots": 700.0,
              "black_beans_dry": 500.0, "bread_whole_wheat": 800.0,
              "peanut_butter": 300.0, "canola_oil": 200.0}
    result = result_from_demand(demand, profile, 60.0, 7, foods_by_id, seed_quotes, nutrition)
    assert result.items and result.budget_status is BudgetStatus.WITHIN
    return result, profile


@pytest.fixture
def relaxed_result(foods_by_id, seed_quotes, nutrition, la_family_profile):
    # A tiny, cheap basket for a 4-person week: it fits the budget but comes
    # nowhere near the nutrition target -> nutrition cannot be met.
    demand = {"rice_white": 500.0, "carrots": 300.0}
    result = result_from_demand(demand, la_family_profile, 30.0, 7, foods_by_id, seed_quotes,
                                nutrition)
    assert result.budget_status is BudgetStatus.WITHIN and not result.nutrition_feasible
    return result, la_family_profile


def all_strings(explanation) -> list[str]:
    return [
        explanation.summary,
        explanation.budget_tradeoffs,
        explanation.food_group_coverage,
        explanation.life_impact,
        *explanation.nutrition_gaps,
        *explanation.item_reasons.keys(),
        *explanation.item_reasons.values(),
    ]


class TestLocalExplanations:
    async def test_populated_for_feasible_result(self, feasible_result):
        result, profile = feasible_result
        explanation = await LocalExplanationService().explain(result, profile)
        assert explanation.generated_by == "local"
        assert f"${result.total_cost:.2f}" in explanation.summary
        assert "Estimated planning total".lower() in explanation.summary.lower()
        assert set(explanation.item_reasons) == {item.food.name for item in result.items}
        assert len(explanation.nutrition_gaps) == len(result.gaps)
        assert explanation.food_group_coverage.startswith("Covers")
        assert explanation.life_impact

    async def test_honest_for_relaxed_result(self, relaxed_result):
        result, profile = relaxed_result
        explanation = await LocalExplanationService().explain(result, profile)
        assert "could not be met" in explanation.summary
        for constraint in result.relaxed_constraints:
            assert constraint in explanation.budget_tradeoffs
        assert explanation.nutrition_gaps

    async def test_deterministic(self, feasible_result):
        result, profile = feasible_result
        service = LocalExplanationService()
        first = await service.explain(result, profile)
        second = await service.explain(result, profile)
        assert first == second

    async def test_no_links_or_medical_claims(self, feasible_result, relaxed_result):
        service = LocalExplanationService()
        for result, profile in (feasible_result, relaxed_result):
            explanation = await service.explain(result, profile)
            for text in all_strings(explanation):
                lowered = text.lower()
                assert "http" not in lowered
                assert "www." not in lowered
                for banned in ("cure", "treat", "diagnos", "prevent disease", "heal "):
                    assert banned not in lowered


def valid_payload(result) -> dict:
    return {
        "summary": "A budget-friendly basket for the week.",
        "item_reasons": [
            {"food_name": item.food.name, "reason": "Affordable and versatile."}
            for item in result.items
        ],
        "nutrition_gaps": ["Covers 48% of the 7-day Vitamin D target."],
        "budget_tradeoffs": "Most of the budget went to staple foods.",
        "food_group_coverage": "Covers 5 of 6 food groups.",
        "life_impact": "About 80 meal portions for the week.",
    }


class TestOpenAIExplanations:
    async def test_happy_path(self, feasible_result):
        result, profile = feasible_result
        service = OpenAIExplanationService(
            "sk-test", LocalExplanationService(), openai_client(valid_payload(result))
        )
        explanation = await service.explain(result, profile)
        assert explanation.generated_by == "openai"
        assert explanation.summary == "A budget-friendly basket for the week."

    async def test_http_error_falls_back_to_local(self, feasible_result):
        result, profile = feasible_result
        service = OpenAIExplanationService(
            "sk-test", LocalExplanationService(), openai_client(status=500)
        )
        explanation = await service.explain(result, profile)
        assert explanation.generated_by == "local"

    async def test_timeout_falls_back_to_local(self, feasible_result):
        result, profile = feasible_result
        service = OpenAIExplanationService(
            "sk-test", LocalExplanationService(), openai_client(raise_timeout=True)
        )
        explanation = await service.explain(result, profile)
        assert explanation.generated_by == "local"

    async def test_invalid_json_falls_back_to_local(self, feasible_result):
        result, profile = feasible_result
        service = OpenAIExplanationService(
            "sk-test", LocalExplanationService(), openai_client("this is not json {")
        )
        explanation = await service.explain(result, profile)
        assert explanation.generated_by == "local"

    async def test_missing_fields_fall_back_to_local(self, feasible_result):
        result, profile = feasible_result
        service = OpenAIExplanationService(
            "sk-test", LocalExplanationService(), openai_client({"summary": "only"})
        )
        explanation = await service.explain(result, profile)
        assert explanation.generated_by == "local"

    async def test_hallucinated_food_falls_back_to_local(self, feasible_result):
        result, profile = feasible_result
        payload = valid_payload(result)
        payload["item_reasons"].append({"food_name": "Caviar", "reason": "Fancy."})
        service = OpenAIExplanationService(
            "sk-test", LocalExplanationService(), openai_client(payload)
        )
        explanation = await service.explain(result, profile)
        assert explanation.generated_by == "local"

    async def test_embedded_link_falls_back_to_local(self, feasible_result):
        result, profile = feasible_result
        payload = valid_payload(result)
        payload["budget_tradeoffs"] = "Buy more at https://example.com/deals"
        service = OpenAIExplanationService(
            "sk-test", LocalExplanationService(), openai_client(payload)
        )
        explanation = await service.explain(result, profile)
        assert explanation.generated_by == "local"


class TestFactory:
    def test_no_key_returns_local(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        profile = HouseholdProfile()
        service = get_explanation_service(profile, openai_client({}))
        assert isinstance(service, LocalExplanationService)

    def test_key_in_profile_returns_openai(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        profile = HouseholdProfile(api_keys={"openai_api_key": "sk-test"})
        service = get_explanation_service(profile, openai_client({}))
        assert isinstance(service, OpenAIExplanationService)

    def test_key_in_env_returns_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        service = get_explanation_service(HouseholdProfile(), openai_client({}))
        assert isinstance(service, OpenAIExplanationService)
