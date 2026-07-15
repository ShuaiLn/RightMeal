"""OpenAI-backed cooking steps for a planned meal.

Given a meal's verified facts (name, per-ingredient raw and cooked grams,
household servings) and the profile's dietary restrictions, the model writes
3–8 short numbered steps. Results are cached persistently (RecipeStore) keyed
by a versioned signature, so a meal's steps generate once and then render
instantly and offline.

There is no local fallback: any failure returns None and the dialog shows a
muted placeholder instead. Steps are display-only text — they never touch
inventory, tracking, or the plan.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx

from models.food import Food
from models.meals import Meal
from models.profile import HouseholdProfile
from services.keys import resolve_key

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT_SECONDS = 20.0

# Bump to invalidate every cached recipe (prompt or format changes).
RECIPE_SCHEMA_VERSION = 1

MAX_STEP_CHARS = 300

SYSTEM_PROMPT = (
    "You write short, practical cooking steps for a home cook preparing one "
    "household meal. Rules you must follow strictly:\n"
    "1. Use ONLY the listed core ingredients. Water, salt, oil and basic "
    "seasonings may be mentioned as optional. Do not introduce any other "
    "ingredient.\n"
    "2. Do not conflict with the stated dietary restrictions.\n"
    "3. Write 3 to 8 numbered-order steps, each one or two plain sentences. "
    "Use the given gram amounts; the whole recipe serves the stated number "
    "of people.\n"
    "4. Never include links or URLs.\n"
    "Respond with JSON matching the provided schema."
)

RESPONSE_SCHEMA = {
    "name": "cooking_steps",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 8,
            },
        },
        "required": ["steps"],
    },
}

# Never flagged as foreign ingredients, even when the catalog sells them.
BASIC_SEASONING_ALLOWLIST = {
    "salt", "pepper", "black pepper", "white pepper", "oil", "cooking oil",
    "olive oil", "water", "sugar", "vinegar", "soy sauce", "garlic", "onion",
    "herbs", "spices", "chili flakes", "lemon juice",
}


@dataclass(frozen=True)
class RecipeRequest:
    """The verified facts the model may reason about."""

    template_id: str
    meal_name: str
    servings: int
    # (food_id, food_name, raw/purchased grams, cooked/display grams or None)
    portions: tuple[tuple[str, str, float, float | None], ...]
    restrictions: tuple[str, ...] = ()
    locale: str = "en"


def build_recipe_request(meal: Meal, profile: HouseholdProfile | None) -> RecipeRequest:
    portions: dict[str, list] = {}
    for portion in meal.portions:
        entry = portions.setdefault(
            portion.food.id,
            [portion.food.id, portion.food.name, 0.0, None],
        )
        entry[2] = round(entry[2] + portion.grams, 1)
        if portion.cooked_grams is not None:
            entry[3] = round((entry[3] or 0.0) + portion.cooked_grams, 1)
    return RecipeRequest(
        template_id=meal.template_id,
        meal_name=meal.name,
        servings=max(profile.total_members, 1) if profile is not None else 1,
        portions=tuple(tuple(entry) for entry in portions.values()),
        restrictions=profile_restrictions(profile),
    )


def profile_restrictions(profile: HouseholdProfile | None) -> tuple[str, ...]:
    """Normalized dietary restrictions — also part of the cache key, so a
    changed restriction never serves a stale recipe."""
    if profile is None:
        return ()
    restrictions: list[str] = []
    if profile.vegetarian:
        restrictions.append("vegetarian")
    if profile.no_pork:
        restrictions.append("no pork")
    if profile.lactose_free:
        restrictions.append("lactose-free")
    restrictions.extend(f"allergy: {allergy}" for allergy in sorted(profile.allergies))
    return tuple(restrictions)


def recipe_cache_key(request: RecipeRequest) -> str:
    """Stable signature of everything that shapes the generated steps."""
    restrictions_fingerprint = hashlib.sha1(
        json.dumps([sorted(request.restrictions), request.locale]).encode("utf-8")
    ).hexdigest()
    payload = json.dumps(
        {
            "version": RECIPE_SCHEMA_VERSION,
            "template_id": request.template_id,
            "meal_name": request.meal_name,
            "servings": request.servings,
            "portions": sorted(
                (fid, round(grams), round(cooked or 0.0))
                for fid, _name, grams, cooked in request.portions
            ),
            "restrictions": restrictions_fingerprint,
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def serialize_request(request: RecipeRequest) -> dict:
    return {
        "meal_name": request.meal_name,
        "serves": request.servings,
        "ingredients": [
            {
                "food_name": name,
                "raw_grams": grams,
                **({"cooked_grams": cooked} if cooked is not None else {}),
            }
            for _fid, name, grams, cooked in request.portions
        ],
        "dietary_restrictions": list(request.restrictions),
    }


class RecipeService:
    def __init__(self, api_key: str, http_client: httpx.AsyncClient,
                 catalog: tuple[Food, ...] = (), model: str = DEFAULT_MODEL):
        self._api_key = api_key
        self._client = http_client
        self._catalog_names = tuple(food.name for food in catalog)
        self._model = model

    async def generate(self, request: RecipeRequest) -> list[str] | None:
        """The cooking steps, or None on ANY failure — the caller shows a
        placeholder instead."""
        try:
            return await self._generate_via_api(request)
        except Exception:  # noqa: BLE001 - any failure means no steps
            return None

    async def _generate_via_api(self, request: RecipeRequest) -> list[str] | None:
        response = await self._client.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(serialize_request(request))},
                ],
                "response_format": {"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        return self._validate(data, request, self._catalog_names)

    @staticmethod
    def _validate(
        data: dict, request: RecipeRequest, catalog_names: tuple[str, ...]
    ) -> list[str] | None:
        """Coarse error catch, not an NLP parser: empty/oversized steps, URLs,
        and blatant foreign ingredients (full multi-word catalog names only —
        never single-common-word substring matches) reject the response."""
        if not isinstance(data, dict):
            return None
        steps = data.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            return None
        cleaned: list[str] = []
        for step in steps:
            if not isinstance(step, str):
                return None
            text = step.strip()
            if not text or len(text) > MAX_STEP_CHARS:
                return None
            lowered = text.lower()
            if "http" in lowered or "www." in lowered:
                return None
            cleaned.append(text)
        joined = " ".join(step.lower() for step in cleaned)
        meal_names = {name.lower() for _fid, name, _g, _c in request.portions}
        for catalog_name in catalog_names:
            lowered = catalog_name.lower()
            if lowered in meal_names or lowered in BASIC_SEASONING_ALLOWLIST:
                continue
            if len(lowered.split()) < 2:
                continue  # single common words ("pepper", "apple") are never matched
            if lowered in joined:
                return None  # names a catalog food that is not in this meal
        return cleaned


def get_recipe_service(
    profile: HouseholdProfile | None,
    http_client: httpx.AsyncClient,
    catalog: tuple[Food, ...] = (),
) -> RecipeService | None:
    """A service when an OpenAI key is configured, else None (placeholder)."""
    api_key = resolve_key("openai_api_key", profile)
    if not api_key:
        return None
    return RecipeService(api_key, http_client=http_client, catalog=catalog)
