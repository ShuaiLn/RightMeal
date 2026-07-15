"""Resolve a parsed ingredient name to a canonical food id.

Hierarchy (first hit wins), per the approved design:

    exact catalog name / search term
    -> curated alias (ingredient_aliases.json)
    -> generic same-class token match
    -> parent-category default
    -> per-recipe override (ingredient_overrides.json, applied by the caller)
    -> unresolved

The resolver is catalog-driven: it takes the known canonical ids (seed +
extended registry) plus their aliases, so the SAME resolver works before and
after the catalog is expanded. A resolved id whose food has no nutrition yet
(``pending`` in the registry) still counts as "known" for classification but
is reported as nutrition-unavailable by the nutrition step.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_STOPWORDS = {
    "fresh", "dried", "ground", "whole", "large", "small", "medium", "ripe",
    "boneless", "skinless", "raw", "cooked", "frozen", "canned", "organic",
    "chopped", "minced", "diced", "sliced", "grated", "shredded", "peeled",
    "of", "the", "a", "an", "some", "for", "to", "taste", "your", "favorite",
    "good", "quality", "extra", "virgin", "pure", "plain", "unsalted", "salted",
    "low", "sodium", "reduced", "fat", "free", "light", "dark", "hot", "cold",
    "warm", "finely", "coarsely", "roughly", "thinly", "thickly", "lean",
}


def normalize(text: str) -> str:
    """NFKD-fold, lowercase, drop punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("oes"):
        return word[:-2]
    if len(word) > 2 and word.endswith("es") and word[-3] in "sxz":
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _key_variants(name: str) -> list[str]:
    """Normalized forms to try: full, singularized, and content-word subsets."""
    norm = normalize(name)
    if not norm:
        return []
    words = norm.split()
    sing = " ".join(_singular(w) for w in words)
    content = [w for w in words if w not in _STOPWORDS]
    content_sing = [_singular(w) for w in content]
    variants = [norm, sing, " ".join(content), " ".join(content_sing)]
    # Also the last content word alone (e.g. "golden delicious apples" -> "apple").
    if content_sing:
        variants.append(content_sing[-1])
    seen: list[str] = []
    for v in variants:
        v = v.strip()
        if v and v not in seen:
            seen.append(v)
    return seen


@dataclass(frozen=True)
class Resolution:
    food_id: str | None
    match_method: str   # exact | alias | generic | category_default | unresolved
    confidence: float


class IngredientResolver:
    def __init__(
        self,
        catalog_terms: dict[str, str],       # normalized term -> food_id (seed names/search terms)
        aliases: dict[str, str],             # normalized alias -> food_id (curated)
        category_defaults: dict[str, str],   # parent_category -> default food_id
        known_ids: set[str],                 # every canonical id (seed + registry)
    ):
        self._exact = dict(catalog_terms)
        # Index aliases by their normalized form AND a singularized form, so a
        # plural alias ("yellow onions") still matches a singular query and
        # vice versa. First write wins on collisions.
        self._alias: dict[str, str] = {}
        for k, v in aliases.items():
            norm = normalize(k)
            sing = " ".join(_singular(w) for w in norm.split())
            for key in (norm, sing):
                self._alias.setdefault(key, v)
        self._category_defaults = category_defaults
        self._known = known_ids

    def resolve(self, name: str) -> Resolution:
        variants = _key_variants(name)
        # 1. exact catalog term
        for i, v in enumerate(variants):
            if v in self._exact:
                return Resolution(self._exact[v], "exact", 1.0 - 0.05 * i)
        # 2. curated alias
        for i, v in enumerate(variants):
            if v in self._alias:
                fid = self._alias[v]
                return Resolution(fid, "alias", 0.92 - 0.05 * i)
        # 3. generic single-token containment against catalog terms
        for v in variants:
            token = v.split()[-1] if v else ""
            if token and token in self._exact:
                return Resolution(self._exact[token], "generic", 0.7)
        # 4. parent-category default via alias-to-category isn't attempted here;
        #    category defaults are keyed by category, applied by roles/overrides.
        return Resolution(None, "unresolved", 0.0)
