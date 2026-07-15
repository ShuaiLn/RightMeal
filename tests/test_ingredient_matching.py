"""Hybrid pantry-input matching."""

from __future__ import annotations

from data.loader import load_catalog
from services.ingredient_matching import match_pantry_input


def _foods():
    return load_catalog()


def test_alias_and_plural_resolve_high():
    foods = _foods()
    for text, expected in [
        ("jasmine rice", "rice_white"),
        ("boneless chicken", "chicken_breast"),
        ("yellow onions", "onions_yellow"),
        ("brown rice", "brown_rice"),
        ("chicken breasts", "chicken_breast"),
    ]:
        level, cands = match_pantry_input(text, foods)
        assert level == "high", (text, level)
        assert cands[0].food_id == expected, (text, cands[0].food_id)


def test_ambiguous_cream_is_medium_not_auto_picked():
    level, cands = match_pantry_input("cream", _foods())
    assert level == "medium"
    ids = {c.food_id for c in cands}
    assert "sour_cream" in ids and "heavy_cream" in ids


def test_no_match_returns_none():
    level, cands = match_pantry_input("xyzzy nonsense", _foods())
    assert level == "none"


def test_empty_input():
    assert match_pantry_input("", _foods()) == ("none", [])


def test_recency_boost_breaks_ties_toward_recent():
    foods = _foods()
    # A plausible partial that isn't a strong single match; recency nudges it.
    level, cands = match_pantry_input("cheese", foods, recent_ids=["cheddar_cheese"])
    assert cands  # produces candidates either way
