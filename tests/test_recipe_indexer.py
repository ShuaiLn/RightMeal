"""Unit tests for the dev-time recipe indexer (scripts/recipe_indexer)."""

# The indexer is a script package added to ``sys.path`` below.
# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from recipe_indexer.ingredient_parser import parse_ingredient_line
from recipe_indexer.resolver import IngredientResolver, normalize
from recipe_indexer.unlock import RecipeGap, rank_ingredients


# -- ingredient parsing -----------------------------------------------------

def test_parses_quantity_unit_name_prep():
    p = parse_ingredient_line("1/2 lb Beef, cut into strips")
    assert p.quantity == 0.5 and p.unit == "lb"
    assert p.name == "beef"
    assert "cut into strips" in p.prep_notes
    assert round(p.grams_explicit) == 227  # 0.5 lb, NOT the fraction denominator


def test_parses_tightly_attached_metric_units():
    cases = {
        "500g flour": (500.0, "g", 500.0, None, "flour"),
        "200ml milk": (200.0, "ml", None, 200.0, "milk"),
        "1kg potatoes": (1.0, "kg", 1000.0, None, "potatoes"),
    }
    for text, expected in cases.items():
        parsed = parse_ingredient_line(text)
        assert (
            parsed.quantity,
            parsed.unit,
            parsed.grams_explicit,
            parsed.ml_explicit,
            parsed.name,
        ) == expected
def test_unicode_fraction_and_parenthetical_metric():
    assert parse_ingredient_line("3⁄4 rolled oats").quantity == 0.75
    p = parse_ingredient_line("2 cans (28 oz.) plum tomatoes")
    assert p.unit == "cans" and round(p.grams_explicit) == 1588  # 2 x 28 oz


def test_optional_and_to_taste():
    assert parse_ingredient_line("Salt to taste").optional
    assert parse_ingredient_line("oil for frying").optional


def test_stacked_descriptors_stripped():
    assert parse_ingredient_line("¼ cup chopped fresh basil").name == "basil"


def test_prep_state_detection():
    assert parse_ingredient_line("1 cup cooked rice").state_hint == "cooked"
    assert parse_ingredient_line("1 can black beans, drained").state_hint == "drained"


# -- resolution -------------------------------------------------------------

def _resolver():
    catalog_terms = {"chicken breast": "chicken_breast", "brown rice": "brown_rice"}
    aliases = {"boneless chicken": "chicken_breast", "jasmine rice": "rice_jasmine",
               "yellow onions": "onions_yellow"}
    known = {"chicken_breast", "brown_rice", "rice_jasmine", "onions_yellow"}
    return IngredientResolver(catalog_terms, aliases, {}, known)


def test_resolver_exact_alias_and_plural():
    r = _resolver()
    assert r.resolve("chicken breast").food_id == "chicken_breast"
    assert r.resolve("boneless chicken").food_id == "chicken_breast"
    assert r.resolve("yellow onion").food_id == "onions_yellow"  # singularized alias
    assert r.resolve("unicorn meat").food_id is None


def test_star_anise_is_not_mapped_to_red_pepper_flakes():
    data_dir = _SCRIPTS.parent / "src" / "data"
    aliases = json.loads(
        (data_dir / "ingredient_aliases.json").read_text(encoding="utf-8")
    )["aliases"]
    registry = json.loads(
        (data_dir / "ingredient_registry.json").read_text(encoding="utf-8")
    )["roles"]
    resolver = IngredientResolver({}, aliases, {}, set(registry))

    resolution = resolver.resolve("star anise")
    assert resolution.food_id == "star_anise"
    assert resolution.food_id != "red_pepper_flakes"
    assert registry[resolution.food_id]["is_seasoning"] is True


def test_normalize_folds_accents_and_case():
    assert normalize("Crème Fraîche") == "creme fraiche"


# -- unlock ranking ---------------------------------------------------------

def test_unlock_ranking_prefers_high_impact_and_handles_multigap():
    # r1 blocked by {a}, r2 by {a,b}, r3 by {a,b,c}. 'a' unlocks r1 immediately
    # and reduces gaps in r2,r3 -> must rank first even though no single pick
    # fully unlocks r2 or r3 yet.
    gaps = [
        RecipeGap("r1", ("dinner",), {"a"}, False),
        RecipeGap("r2", ("dinner",), {"a", "b"}, False),
        RecipeGap("r3", ("dinner",), {"a", "b", "c"}, False),
    ]
    registry = {"a": {"role": "protein"}, "b": {"role": "vegetable"}, "c": {"role": "fat"}}
    ranked = rank_ingredients(gaps, registry, limit=3)
    assert ranked[0].ingredient_id == "a"
    assert ranked[0].immediate_unlocks == 1


def test_unlock_ranking_skips_recipes_with_unresolved_core():
    gaps = [RecipeGap("r1", ("lunch",), {"a"}, True)]  # has an unresolved-alias core
    ranked = rank_ingredients(gaps, {"a": {"role": "protein"}}, limit=5)
    assert ranked == []
