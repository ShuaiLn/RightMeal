"""Iterative recipe-unlock ranking (greedy set cover with partial credit).

Picks which pending ingredients to give reviewed nutrition first, so the most
main-meal / breakfast recipes become auto-plannable. NOT raw frequency: a
recipe missing several core ingredients would score every single one 0 for
"immediate full unlock", so the score also credits total gap reduction,
"one-away" progress, and core-role importance (review).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Core-role weight: unlocking a main protein/carb matters more than a minor fat.
_ROLE_WEIGHT = {
    "protein": 3.0, "main_carb": 3.0, "vegetable": 2.0,
    "dairy": 1.5, "fat": 1.2, "sweetener": 1.0, "sauce": 0.8, "liquid": 0.5,
}

# Score weights for the greedy objective.
_W_UNLOCK = 100.0      # recipes fully unlocked this pick
_W_ONE_AWAY = 8.0      # recipes brought to a single remaining gap
_W_GAP_REDUCE = 1.0    # each recipe-gap this ingredient removes
_W_ROLE = 2.0          # core-role importance of the ingredient


@dataclass
class RecipeGap:
    recipe_id: str
    slot_types: tuple[str, ...]
    pending_core_ids: set[str]      # resolvable ids that lack nutrition
    has_unresolved_core: bool       # truly unresolved -> needs alias work, not USDA


@dataclass
class RankedIngredient:
    ingredient_id: str
    role: str
    score: float
    immediate_unlocks: int
    recipes_touched: int
    step: int


def _role_of(ing_id: str, registry: dict) -> str:
    return registry.get(ing_id, {}).get("role", "sauce")


def rank_ingredients(
    gaps: list[RecipeGap],
    registry: dict,
    limit: int = 40,
) -> list[RankedIngredient]:
    # Only recipes that CAN be unlocked by adding nutrition (no truly-unresolved
    # core, at least one pending core id) participate.
    live = [g for g in gaps if g.pending_core_ids and not g.has_unresolved_core]
    remaining = {g.recipe_id: set(g.pending_core_ids) for g in live}
    ranked: list[RankedIngredient] = []
    chosen: set[str] = set()

    for step in range(1, limit + 1):
        # Candidate ingredient ids still blocking something.
        candidates: set[str] = set()
        for ids in remaining.values():
            candidates |= ids
        candidates -= chosen
        if not candidates:
            break

        best: RankedIngredient | None = None
        for cand in sorted(candidates):
            immediate = 0
            one_away = 0
            gap_reduce = 0
            touched = 0
            for ids in remaining.values():
                if cand in ids:
                    touched += 1
                    gap_reduce += 1
                    if len(ids) == 1:
                        immediate += 1
                    elif len(ids) == 2:
                        one_away += 1
            role = _role_of(cand, registry)
            score = (
                _W_UNLOCK * immediate
                + _W_ONE_AWAY * one_away
                + _W_GAP_REDUCE * gap_reduce
                + _W_ROLE * _ROLE_WEIGHT.get(role, 0.5)
            )
            cand_ranked = RankedIngredient(cand, role, round(score, 2), immediate, touched, step)
            if best is None or (cand_ranked.score, -ord(cand[0])) > (best.score, -ord(best.ingredient_id[0])):
                best = cand_ranked

        assert best is not None
        ranked.append(best)
        chosen.add(best.ingredient_id)
        # Remove the chosen ingredient from every recipe's remaining gap set;
        # recipes now fully covered drop out.
        for rid in list(remaining):
            remaining[rid].discard(best.ingredient_id)
            if not remaining[rid]:
                del remaining[rid]
        if not remaining:
            break

    return ranked
