"""Session-wide application state."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from data import load_nutrient_targets, load_seed_foods
from models import Food, HouseholdProfile
from services.cache import SessionCache
from services.nutrition import NutritionService
from services.profile_store import ProfileStore


@dataclass
class AppState:
    store: ProfileStore
    profile: HouseholdProfile | None = None
    cache: SessionCache = field(default_factory=SessionCache)
    http_client: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)
    foods: tuple[Food, ...] = field(default_factory=load_seed_foods)
    nutrition: NutritionService = field(
        default_factory=lambda: NutritionService(load_nutrient_targets())
    )
