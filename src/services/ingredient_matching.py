"""Hybrid matching for manual pantry input.

Maps free-typed ingredient text to a catalog food via canonical names, curated
aliases, plural/spelling variation, and recency — returning a confidence level
so the UI can confirm a single strong match, offer a few for an ambiguous one,
or fall back to a custom item. Ambiguous input ("cream") is never auto-picked.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, Sequence

from models.food import Food
from services.matching import match_confidence

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

HIGH = 0.82
MEDIUM = 0.5

Level = Literal["high", "medium", "none"]


@dataclass(frozen=True)
class MatchCandidate:
    food_id: str
    display: str
    score: float


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold().strip()


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 2 and word.endswith("es") and word[-3] in "sxz":
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    try:
        data = json.loads((_DATA_DIR / "ingredient_aliases.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    out: dict[str, str] = {}
    for alias, fid in data.get("aliases", {}).items():
        norm = _normalize(alias)
        sing = " ".join(_singular(w) for w in norm.split())
        out.setdefault(norm, fid)
        out.setdefault(sing, fid)
    return out


def match_pantry_input(
    text: str,
    foods: Sequence[Food],
    recent_ids: Sequence[str] = (),
) -> tuple[Level, list[MatchCandidate]]:
    """(level, candidates). high: one confident match; medium: 3-5 choices;
    none: no reliable match (offer a custom item)."""
    norm = _normalize(text)
    if not norm:
        return "none", []
    sing = " ".join(_singular(w) for w in norm.split())
    by_id = {f.id: f for f in foods}

    # 1. Exact canonical name or alias -> high confidence, single match.
    alias = _aliases()
    for key in (norm, sing):
        if key in alias and alias[key] in by_id:
            f = by_id[alias[key]]
            return "high", [MatchCandidate(f.id, f.name, 1.0)]
        exact = [f for f in foods if _normalize(f.name) == key]
        if len(exact) == 1:
            return "high", [MatchCandidate(exact[0].id, exact[0].name, 1.0)]

    # 2. Fuzzy score against name + search terms.
    scored: list[MatchCandidate] = []
    for f in foods:
        score = match_confidence((f.name, *f.search_terms), text)
        if f.id in recent_ids:
            score = min(1.0, score + 0.05)
        if score > 0.2:
            scored.append(MatchCandidate(f.id, f.name, score))
    scored.sort(key=lambda c: (-c.score, c.display))

    if not scored:
        return "none", []
    top = scored[0]
    runner = scored[1].score if len(scored) > 1 else 0.0
    if top.score >= HIGH and top.score - runner >= 0.1:
        return "high", [top]
    if top.score >= MEDIUM:
        return "medium", scored[:5]
    return "none", scored[:5]
