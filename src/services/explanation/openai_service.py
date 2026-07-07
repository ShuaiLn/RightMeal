"""Optional OpenAI-backed explanations with strict validation.

The model only rephrases a verified optimizer result. Any failure — missing
key, HTTP error, timeout, invalid JSON, missing fields, hallucinated food
names, or embedded links — falls back to the deterministic local templates.
"""

from __future__ import annotations

import json

import httpx

from models.basket import OptimizationResult
from models.explanation import Explanation
from models.food import FOOD_GROUP_LABELS, Nutrients
from models.pricing import PRICE_SOURCE_LABELS
from models.profile import HouseholdProfile
from services.explanation.base import ExplanationService
from services.explanation.local import LocalExplanationService

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT_SECONDS = 20.0

SYSTEM_PROMPT = (
    "You explain a grocery basket that has already been chosen and verified by a "
    "planning tool. Rules you must follow strictly:\n"
    "1. Only describe the provided basket. Never add, remove, or substitute foods.\n"
    "2. Never make medical claims, diagnoses, treatment suggestions, or promises "
    "of health outcomes. Use neutral coverage language like 'covers 74% of the "
    "planning target'.\n"
    "3. Never include links, URLs, or shopping instructions.\n"
    "4. Write plain, friendly English for a general audience.\n"
    "Respond with JSON matching the provided schema."
)

RESPONSE_SCHEMA = {
    "name": "basket_explanation",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "item_reasons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "food_name": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["food_name", "reason"],
                },
            },
            "nutrition_gaps": {"type": "array", "items": {"type": "string"}},
            "budget_tradeoffs": {"type": "string"},
            "food_group_coverage": {"type": "string"},
            "life_impact": {"type": "string"},
        },
        "required": [
            "summary",
            "item_reasons",
            "nutrition_gaps",
            "budget_tradeoffs",
            "food_group_coverage",
            "life_impact",
        ],
    },
}


def serialize_result(result: OptimizationResult, profile: HouseholdProfile) -> dict:
    """Compact, verified facts the model is allowed to describe."""
    return {
        "estimated_planning_total_usd": result.total_cost,
        "budget_usd": result.budget,
        "horizon_days": result.horizon_days,
        "household_members": profile.total_members,
        "budget_feasible": result.budget_feasible,
        "nutrition_feasible": result.nutrition_feasible,
        "items": [
            {
                "food_name": item.food.name,
                "quantity": item.quantity_label,
                "cost_usd": round(item.cost, 2),
                "food_group": FOOD_GROUP_LABELS[item.food.food_group],
                "price_source": PRICE_SOURCE_LABELS[item.quote.source],
            }
            for item in result.items
        ],
        "food_groups_covered": [
            FOOD_GROUP_LABELS[group] for group in result.group_coverage
        ],
        "nutrition_gaps": [
            {
                "nutrient": Nutrients.NUTRIENT_LABELS[gap.nutrient],
                "percent_of_target": round(gap.pct, 1),
            }
            for gap in result.gaps
        ],
        "tradeoffs": list(result.relaxed_constraints),
        "dominance_flags": list(result.dominance_flags),
    }


class OpenAIExplanationService(ExplanationService):
    def __init__(
        self,
        api_key: str,
        fallback: LocalExplanationService,
        http_client: httpx.AsyncClient,
        model: str = DEFAULT_MODEL,
    ):
        self._api_key = api_key
        self._fallback = fallback
        self._client = http_client
        self._model = model

    async def explain(self, result: OptimizationResult, profile: HouseholdProfile) -> Explanation:
        try:
            explanation = await self._explain_via_api(result, profile)
        except Exception:  # noqa: BLE001 - any failure means local fallback
            explanation = None
        if explanation is not None:
            return explanation
        return await self._fallback.explain(result, profile)

    async def _explain_via_api(
        self, result: OptimizationResult, profile: HouseholdProfile
    ) -> Explanation | None:
        response = await self._client.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(serialize_result(result, profile)),
                    },
                ],
                "response_format": {"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        return self._validate(data, result)

    @staticmethod
    def _validate(data: dict, result: OptimizationResult) -> Explanation | None:
        required = (
            "summary",
            "item_reasons",
            "nutrition_gaps",
            "budget_tradeoffs",
            "food_group_coverage",
            "life_impact",
        )
        if not isinstance(data, dict) or any(key not in data for key in required):
            return None

        basket_names = {item.food.name for item in result.items}
        item_reasons: dict[str, str] = {}
        for entry in data["item_reasons"]:
            name = entry.get("food_name", "")
            if name not in basket_names:
                return None  # hallucinated food -> reject the whole response
            item_reasons[name] = str(entry.get("reason", ""))

        strings = [
            str(data["summary"]),
            str(data["budget_tradeoffs"]),
            str(data["food_group_coverage"]),
            str(data["life_impact"]),
            *[str(g) for g in data["nutrition_gaps"]],
            *item_reasons.values(),
        ]
        if any("http" in text.lower() or "www." in text.lower() for text in strings):
            return None  # no links, ever

        return Explanation(
            summary=str(data["summary"]),
            item_reasons=item_reasons,
            nutrition_gaps=[str(g) for g in data["nutrition_gaps"]],
            budget_tradeoffs=str(data["budget_tradeoffs"]),
            food_group_coverage=str(data["food_group_coverage"]),
            life_impact=str(data["life_impact"]),
            generated_by="openai",
        )
