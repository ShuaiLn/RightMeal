"""Identity-safe deterministic local Pantry matching."""

from dataclasses import replace

import numpy as np

from models.photo_analysis import FoodForm, ProductFacts
from services.pantry_matcher import (
    BUNDLED_MODEL_PATH,
    CatalogMatcher,
    identity_query,
    lexical_score,
)


def product(name: str, form: FoodForm, brand=None, observed=None, package=None):
    return ProductFacts(
        observed_name=observed or name,
        generic_food_name=name,
        brand=brand,
        language="en",
        form=form,
        package_text=package,
        quantity=1,
        total_weight=None,
        unit_weight=None,
        printed_price=None,
        printed_currency=None,
        visible_evidence=(),
    )


class KeywordEmbedder:
    def __init__(self):
        self.calls = 0

    def embed(self, documents):
        self.calls += 1
        for document in documents:
            text = document.casefold()
            if "white rice" in text or "cereal staple" in text:
                yield [1.0, 0.0, 0.0]
            elif "brown rice" in text:
                yield [0.5, 0.5, 0.0]
            else:
                yield [0.0, 0.0, 1.0]


def test_exact_multilingual_safe_alias_preselects(foods):
    result = CatalogMatcher(foods, embedder=KeywordEmbedder()).match(
        product("arroz blanco", FoodForm.DRY)
    )
    assert result.selected_food_id == "rice_white"
    assert result.candidates[0].exact
    assert result.candidates[0].reason == "Exact Pantry alias"


def test_canned_black_beans_never_map_to_dry_black_beans(foods):
    result = CatalogMatcher(foods, embedder=KeywordEmbedder()).match(
        product(
            "black beans",
            FoodForm.CANNED,
            brand="Goya",
            observed="Goya Black Beans",
            package="15.5 oz can",
        )
    )
    assert result.selected_food_id is None
    assert "black_beans_dry" not in {candidate.food_id for candidate in result.candidates}


def test_kidney_beans_do_not_use_broad_recipe_aliases(foods, tmp_path):
    result = CatalogMatcher(
        foods, bundled_model_path=tmp_path / "missing"
    ).match(product("kidney beans", FoodForm.DRY))
    assert result.selected_food_id is None
    assert not any(candidate.exact for candidate in result.candidates)


def test_semantic_preselection_uses_hybrid_gate_and_margin(foods):
    result = CatalogMatcher(foods, embedder=KeywordEmbedder()).match(
        product("long grain cereal staple", FoodForm.DRY)
    )
    assert result.semantic_available
    assert result.selected_food_id == "rice_white"
    assert result.candidates[0].match_score >= 0.78
    assert result.candidates[0].preselected


def test_unavailable_semantics_never_preselect_non_exact(foods, tmp_path):
    result = CatalogMatcher(
        foods, bundled_model_path=tmp_path / "missing"
    ).match(product("whte rce", FoodForm.DRY))
    assert not result.semantic_available
    assert result.selected_food_id is None
    assert "Semantic matching unavailable" in result.status_message
    assert result.candidates


def test_catalog_embedding_cache_and_signature_invalidation(foods, tmp_path):
    first_embedder = KeywordEmbedder()
    first = CatalogMatcher(foods, embedder=first_embedder, cache_dir=tmp_path)
    first.match(product("cereal staple", FoodForm.DRY))
    assert (tmp_path / "catalog_embeddings.json").is_file()
    first_signature = first.signature

    second_embedder = KeywordEmbedder()
    second = CatalogMatcher(foods, embedder=second_embedder, cache_dir=tmp_path)
    second.match(product("cereal staple", FoodForm.DRY))
    # One call for the query only: catalog vectors came from the signature cache.
    assert second_embedder.calls == 1

    changed = tuple(
        replace(food, name="White rice renamed") if food.id == "rice_white" else food
        for food in foods
    )
    third = CatalogMatcher(changed, embedder=KeywordEmbedder(), cache_dir=tmp_path)
    assert third.signature != first_signature


def test_brand_quantity_and_package_are_removed_from_identity_query():
    query = identity_query(
        "Acme White Rice 2 lb bag",
        brand="Acme",
        package_text="2 lb bag",
    )
    assert query == "white rice"


def test_lexical_formula_is_sequence_plus_token_coverage():
    # Exact strings have both components at 1.0.
    assert lexical_score("white rice", "white rice") == 1.0
    assert 0 < lexical_score("white rice", "rice white") < 1.0


def test_pinned_multilingual_onnx_model_is_bundled():
    assert (BUNDLED_MODEL_PATH / "config.json").is_file()
    assert (BUNDLED_MODEL_PATH / "tokenizer.json").stat().st_size > 1_000_000
    assert (BUNDLED_MODEL_PATH / "model_optimized.onnx").stat().st_size > 200_000_000


def test_numpy_embedding_vectors_are_supported(foods):
    class NumpyEmbedder(KeywordEmbedder):
        def embed(self, documents):
            for vector in super().embed(documents):
                yield np.asarray(vector, dtype=np.float32)

    result = CatalogMatcher(foods, embedder=NumpyEmbedder()).match(
        product("long grain cereal staple", FoodForm.DRY)
    )
    assert result.semantic_available
    assert result.selected_food_id == "rice_white"
