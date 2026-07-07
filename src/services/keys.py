"""API key resolution: in-app profile value first, then environment/.env."""

from __future__ import annotations

import os

from models.profile import HouseholdProfile

ENV_NAMES: dict[str, str] = {
    "kroger_client_id": "KROGER_CLIENT_ID",
    "kroger_client_secret": "KROGER_CLIENT_SECRET",
    "instacart_api_key": "INSTACART_API_KEY",
    "fdc_api_key": "FDC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "bls_api_key": "BLS_API_KEY",
}


def resolve_key(name: str, profile: HouseholdProfile | None = None) -> str | None:
    """Return the API key value, preferring the profile over env vars."""
    if name not in ENV_NAMES:
        raise KeyError(f"Unknown API key name: {name!r}")
    if profile is not None:
        value = profile.api_keys.get(name, "").strip()
        if value:
            return value
    value = os.environ.get(ENV_NAMES[name], "").strip()
    return value or None
