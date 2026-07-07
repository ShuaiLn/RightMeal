"""Deterministic fuzzy matching between seed foods and store product names."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Sequence

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub(" ", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def match_confidence(search_terms: Sequence[str], product_name: str) -> float:
    """Score how well a store product name matches a food's search terms.

    Combines the best character-level similarity (60%) with query-token
    coverage (40%). Pure and deterministic; the price engine rejects scores
    below its confidence threshold.
    """
    if not search_terms or not product_name.strip():
        return 0.0
    product_norm = _normalize(product_name)
    product_tokens = _tokens(product_name)

    best_ratio = 0.0
    best_overlap = 0.0
    for term in search_terms:
        term_norm = _normalize(term)
        if not term_norm:
            continue
        ratio = SequenceMatcher(None, term_norm, product_norm).ratio()
        term_tokens = set(term_norm.split())
        overlap = len(term_tokens & product_tokens) / len(term_tokens) if term_tokens else 0.0
        best_ratio = max(best_ratio, ratio)
        best_overlap = max(best_overlap, overlap)

    return round(0.6 * best_ratio + 0.4 * best_overlap, 4)
