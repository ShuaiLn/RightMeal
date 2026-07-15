"""OpenAI-backed leftover estimation from a one-sentence note.

Given a cooked meal (name + per-ingredient raw grams for the ONE serving the
meal card represents) and the user's note in any language ("剩了大概三分之一",
"we ate all the rice but left the chicken"), the model estimates how much is
left — overall and, when the note names specific foods, per ingredient.

There is no local fallback service: any failure returns None and the UI opens
the manual percentage dialog instead. The result never touches inventory
directly — the caller derives the record's servings from the component grams.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from models.meals import Meal
from models.profile import HouseholdProfile
from services.keys import resolve_key

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT_SECONDS = 20.0

SYSTEM_PROMPT = (
    "You estimate how much of a cooked household meal is left over, from the "
    "ingredients and a short note written by the person who ate it (the note "
    "may be in any language). Rules you must follow strictly:\n"
    "1. Estimate the fraction of the CURRENTLY SERVED meal that remains. The "
    "fraction refers only to the serving represented by this meal — not the "
    "entire batch and not future planned leftover servings.\n"
    "2. leftover_fraction is the overall fraction of the whole serving left, "
    "0.0 (nothing left) to 1.0 (untouched), weighted by amount.\n"
    "3. When the note singles out specific ingredients ('the rice is half "
    "left, the chicken is gone'), report those in components using the "
    "provided food_id values; leave components empty otherwise. Never invent "
    "food ids that are not in the ingredient list.\n"
    "4. If the note does not describe leftovers at all, return null for "
    "leftover_fraction.\n"
    "Respond with JSON matching the provided schema."
)

RESPONSE_SCHEMA = {
    "name": "leftover_estimate",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "leftover_fraction": {"type": ["number", "null"]},
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "food_id": {"type": "string"},
                        "remaining_fraction": {"type": "number"},
                    },
                    "required": ["food_id", "remaining_fraction"],
                },
            },
        },
        "required": ["leftover_fraction", "components"],
    },
}


@dataclass(frozen=True)
class LeftoverEstimate:
    overall_fraction: float
    components: dict[str, float]  # food_id -> remaining fraction overrides


def serialize_meal(meal: Meal, note: str) -> dict:
    """The verified facts the model may reason about — one serving's worth."""
    portions: dict[str, dict] = {}
    for portion in meal.portions:
        entry = portions.setdefault(
            portion.food.id,
            {"food_id": portion.food.id, "food_name": portion.food.name, "grams": 0.0},
        )
        entry["grams"] = round(entry["grams"] + portion.grams, 1)
    return {
        "meal_name": meal.name,
        "is_batch_cooked": bool(meal.batch_id),
        "portions": list(portions.values()),
        "user_note": note,
    }


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


class LeftoverAnalyzer:
    def __init__(self, api_key: str, http_client: httpx.AsyncClient, model: str = DEFAULT_MODEL):
        self._api_key = api_key
        self._client = http_client
        self._model = model

    async def analyze(self, meal: Meal, note: str) -> LeftoverEstimate | None:
        """The estimated leftovers, or None on ANY failure — the caller falls
        back to asking the user for a percentage."""
        try:
            return await self._analyze_via_api(meal, note)
        except Exception:  # noqa: BLE001 - any failure means manual fallback
            return None

    async def _analyze_via_api(self, meal: Meal, note: str) -> LeftoverEstimate | None:
        response = await self._client.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(serialize_meal(meal, note))},
                ],
                "response_format": {"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        return self._validate(data, meal)

    @staticmethod
    def _validate(data: dict, meal: Meal) -> LeftoverEstimate | None:
        if not isinstance(data, dict) or "leftover_fraction" not in data:
            return None
        overall = data["leftover_fraction"]
        if overall is None or isinstance(overall, bool) or not isinstance(overall, (int, float)):
            return None
        known_ids = {portion.food.id for portion in meal.portions}
        components: dict[str, float] = {}
        for entry in data.get("components") or []:
            if not isinstance(entry, dict):
                return None
            food_id = entry.get("food_id")
            fraction = entry.get("remaining_fraction")
            if food_id not in known_ids:
                return None  # hallucinated food -> reject the whole response
            if food_id in components:
                return None  # duplicated component -> ambiguous, reject
            if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
                return None
            components[str(food_id)] = _clamp(float(fraction))
        return LeftoverEstimate(overall_fraction=_clamp(float(overall)), components=components)


def get_leftover_analyzer(
    profile: HouseholdProfile | None, http_client: httpx.AsyncClient
) -> LeftoverAnalyzer | None:
    """An analyzer when an OpenAI key is configured, else None (manual entry)."""
    api_key = resolve_key("openai_api_key", profile)
    if not api_key:
        return None
    return LeftoverAnalyzer(api_key, http_client=http_client)
