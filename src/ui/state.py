"""Session-wide application state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from data import load_nutrient_targets
from data.loader import load_catalog, load_recipe_index
from models import Food, HouseholdProfile, Pantry, SavedPlan
from models.prepared_leftover import PreparedLeftover
from models.recipe import Recipe
from models.purchase_log import PurchaseRecord
from models.photo_import import PhotoImportRecord
from services.cache import SessionCache
from services.image_cache import ImageCache
from services.nutrition import NutritionService
from services.pantry_store import PantryStore
from services.pantry_matcher import CatalogMatcher
from services.plan_store import PlanStore
from services.prepared_leftovers_store import PreparedLeftoversStore
from services.profile_store import ProfileStore
from services.purchase_log_store import PurchaseLogStore
from services.photo_import_store import PhotoImportStore
from services.recipe_store import RecipeStore
from services.tx import TransactionManager
from ui.plan_draft import PlanDraft


@dataclass
class AppState:
    store: ProfileStore
    profile: HouseholdProfile | None = None
    cache: SessionCache = field(default_factory=SessionCache)
    http_client: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)
    foods: tuple[Food, ...] = field(default_factory=load_catalog)
    recipes_catalog: tuple[Recipe, ...] = field(default_factory=load_recipe_index)
    nutrition: NutritionService = field(
        default_factory=lambda: NutritionService(load_nutrient_targets())
    )
    plan_store: PlanStore = None  # type: ignore[assignment]  # built in __post_init__
    saved_plan: SavedPlan | None = None
    pantry_store: PantryStore = None  # type: ignore[assignment]  # built in __post_init__
    pantry: Pantry = field(default_factory=Pantry)
    prepared_leftovers_store: PreparedLeftoversStore = None  # type: ignore[assignment]
    prepared_leftovers: list[PreparedLeftover] = field(default_factory=list)
    recipe_store: RecipeStore = None  # type: ignore[assignment]  # built in __post_init__
    recipes: dict[str, list[str]] = field(default_factory=dict)
    purchase_log_store: PurchaseLogStore = None  # type: ignore[assignment]
    purchase_log: list[PurchaseRecord] = field(default_factory=list)
    # Set when purchases.json failed to load: purchase/undo/receipt mutations
    # are paused for the session — a corrupt log is never a legal empty one.
    purchase_log_error: str | None = None
    photo_import_store: PhotoImportStore = None  # type: ignore[assignment]
    photo_imports: list[PhotoImportRecord] = field(default_factory=list)
    photo_import_error: str | None = None
    image_cache: ImageCache = None  # type: ignore[assignment]  # built in __post_init__
    tx: TransactionManager = None  # type: ignore[assignment]  # built in __post_init__
    # In-progress Start-tab inputs, preserved across a trip to edit the
    # household profile — transient, never persisted to disk.
    plan_draft: PlanDraft | None = None
    # Monotonic token for plan generation: an in-flight generation compares
    # its token before EVERY side effect, so a result computed from inputs
    # the user has since edited can never be persisted or shown.
    generation_seq: int = 0
    # Starting a newer analysis invalidates every older open analysis dialog.
    photo_analysis_seq: int = 0
    pantry_matcher: CatalogMatcher | None = None

    def __post_init__(self) -> None:
        if self.plan_store is None:
            self.plan_store = PlanStore(self.store.base_dir)
        if self.pantry_store is None:
            self.pantry_store = PantryStore(self.store.base_dir)
        if self.prepared_leftovers_store is None:
            self.prepared_leftovers_store = PreparedLeftoversStore(self.store.base_dir)
        if self.recipe_store is None:
            self.recipe_store = RecipeStore(self.store.base_dir)
        if self.purchase_log_store is None:
            self.purchase_log_store = PurchaseLogStore(self.store.base_dir)
        if self.photo_import_store is None:
            self.photo_import_store = PhotoImportStore(self.store.base_dir)
        if self.image_cache is None:
            self.image_cache = ImageCache(self.store.base_dir / "image_cache", self.http_client)
        if self.tx is None:
            self.tx = TransactionManager(self.store.base_dir)

    def persist(
        self,
        *,
        plan: SavedPlan | None = None,
        pantry: Pantry | None = None,
        leftovers: list[PreparedLeftover] | None = None,
        recipes: dict[str, list[str]] | None = None,
        purchases: list[PurchaseRecord] | None = None,
        photo_imports: list[PhotoImportRecord] | None = None,
    ) -> None:
        """Write the given stores in one transaction (all-or-nothing on disk).

        Every store write in the app goes through here — never call the
        stores' save() directly from UI code. Pass only what changed; an
        empty leftovers list still persists (it means "no leftovers now").
        """
        writes: dict[Path, str] = {}
        if plan is not None:
            writes[self.plan_store.path] = self.plan_store.to_json_text(plan)
        if pantry is not None:
            writes[self.pantry_store.path] = self.pantry_store.to_json_text(pantry)
            # The pantry is a generation input: editing it mid-flight must
            # invalidate any in-flight generation even if none is restarted.
            # (Profile saves bypass persist(); their save sites bump directly.)
            self.begin_generation()
        if leftovers is not None:
            writes[self.prepared_leftovers_store.path] = (
                self.prepared_leftovers_store.to_json_text(leftovers)
            )
        if recipes is not None:
            writes[self.recipe_store.path] = self.recipe_store.to_json_text(recipes)
        if purchases is not None:
            writes[self.purchase_log_store.path] = (
                self.purchase_log_store.to_json_text(purchases)
            )
        if photo_imports is not None:
            writes[self.photo_import_store.path] = (
                self.photo_import_store.to_json_text(photo_imports)
            )
        self.tx.save_all(writes)

    def begin_generation(self) -> int:
        """Issue a new generation token; every earlier token becomes stale."""
        self.generation_seq += 1
        return self.generation_seq

    def is_current_generation(self, gen_id: int) -> bool:
        return gen_id == self.generation_seq

    def begin_photo_analysis(self) -> int:
        self.photo_analysis_seq += 1
        return self.photo_analysis_seq

    def is_current_photo_analysis(self, analysis_id: int) -> bool:
        return analysis_id == self.photo_analysis_seq

    @property
    def foods_by_id(self) -> dict[str, Food]:
        return {food.id: food for food in self.foods}

    @property
    def recipes_by_id(self) -> dict[str, Recipe]:
        return {recipe.id: recipe for recipe in self.recipes_catalog}

    @property
    def leftovers_by_id(self) -> dict[str, PreparedLeftover]:
        return {leftover.id: leftover for leftover in self.prepared_leftovers}

    def image_src_for(self, food: Food) -> bytes | str | None:
        """Cached bytes when available, else the web URL (first run online)."""
        if not food.image_url:
            return None
        return self.image_cache.get_cached(food.image_url) or food.image_url

    def user_image_src_for(self, food: Food) -> bytes | str | None:
        """The user's most recent confirmed purchase photo for this food,
        falling back to the catalog image."""
        for record in reversed(self.purchase_log):
            if (
                record.food_id == food.id
                and record.voided_at is None
                and record.origin == "product_photo"
                and record.photo_path
            ):
                path = self.store.base_dir / record.photo_path
                if path.is_file():
                    return str(path)
        return self.image_src_for(food)

    def latest_brand_for(self, food: Food) -> str | None:
        """The brand of the food's most recent non-voided purchase, if any."""
        for record in reversed(self.purchase_log):
            if record.food_id == food.id and record.voided_at is None and record.brand:
                return record.brand
        return None
