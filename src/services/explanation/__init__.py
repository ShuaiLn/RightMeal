"""Explanation services: OpenAI when a key is configured, local otherwise."""

from __future__ import annotations

import httpx

from models.profile import HouseholdProfile
from services.explanation.base import ExplanationService
from services.explanation.local import LocalExplanationService
from services.explanation.openai_service import OpenAIExplanationService
from services.keys import resolve_key

__all__ = [
    "ExplanationService",
    "LocalExplanationService",
    "OpenAIExplanationService",
    "get_explanation_service",
]


def get_explanation_service(
    profile: HouseholdProfile | None,
    http_client: httpx.AsyncClient,
) -> ExplanationService:
    api_key = resolve_key("openai_api_key", profile)
    local = LocalExplanationService()
    if api_key:
        return OpenAIExplanationService(api_key, fallback=local, http_client=http_client)
    return local
