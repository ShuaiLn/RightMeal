"""Conservative classification of equipment and material ingredient lines.

The source corpus occasionally puts cookware or disposable materials in the
ingredient list.  Those lines are useful recipe instructions, but they are not
food demand: they must not be mapped to a catalog food, assigned invented
grams, included in nutrition coverage, or sent to pricing.

This classifier intentionally recognizes only clear standalone tools and
materials.  In particular, package descriptions such as ``a jar of tomatoes``
and food preparations such as ``chicken skewers`` remain food lines.
"""

from __future__ import annotations

import re


_SPACE_RE = re.compile(r"\s+")
_PAREN_RE = re.compile(r"\([^)]*\)")

# A container noun followed by ``of`` describes the amount/package of a food,
# not a requested empty container ("1 container of barbecue rub", "a jar of
# apple sauce").  Check this before the standalone-container rules.
_PACKAGED_FOOD_RE = re.compile(
    r"\b(?:containers?|jars?|tins?|cans?|bottles?|bags?|boxes?|packages?|packets?)\s+of\b",
    re.I,
)

_CLEAR_NONFOOD_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        # Straining/incubation tools used by the cheese and yogurt recipes.
        r"^(?:find\s+)?(?:a\s+)?(?:fine[- ]mesh\s+)?(?:sieves?|strainers?)(?:\s+or\s+(?:a\s+)?cheese\s*cloth)?$",
        r"^(?:find\s+)?(?:a\s+)?(?:sieve\s+or\s+)?cheese\s*cloth$",
        r"^(?:(?:kitchen|candy|instant[- ]read|food)\s+)?thermometers?$",
        r"^(?:vacuum\s+)?thermos(?:es)?(?:\s+(?:flasks?|bottles?))?$",
        # Paper, foil, and wrapping materials.  The restricted paper modifiers
        # deliberately do not match food typos such as "Red Paper Flakes".
        r"^(?:sheets?\s+of\s+)?(?:parchment|baking|wax(?:ed)?|butcher)\s+paper(?:\s+(?:squares?|sheets?|liners?))?(?:\s+.*)?$",
        r"^(?:(?:heavy[- ]duty|aluminum|aluminium|tin)\s+)?foil(?:\s+(?:sheets?|rolls?))?$",
        r"^(?:plastic\s+wrap|cling\s+(?:film|wrap)|wax(?:ed)?\s+paper)$",
        r"^(?:paper\s+towels?|coffee\s+filters?|rubber\s+bands?)$",
        # Bare or material-qualified fasteners are equipment.  A leading food
        # word is not accepted, so "chicken skewers" stays a food line.
        r"^(?:(?:wooden|bamboo|metal)\s+)?skewers?$",
        r"^(?:(?:wooden|bamboo)\s+)?toothpicks?$",
        r"^(?:(?:kitchen|butcher(?:'s|s)?)\s+)?twine$",
        # Empty vessels.  Modifiers are deliberately allowlisted; the package
        # guard above protects "container of ..." and "jar of ..." lines.
        r"^(?:(?:glass|mason|storage|airtight|resealable|freezer[- ]safe|heatproof|food[- ]safe|cheese)\s+)?(?:containers?|jars?|recipients?)$",
        # Other unambiguous standalone equipment present in the corpus.
        r"^(?:oven[- ]safe\s+)?pan$",
        r"^(?:(?:baking|sheet|oven)\s+)?trays?$",
        r"^(?:cooling|wire|oven)\s+racks?$",
        r"^(?:(?:cake|baking|muffin|cupcake)\s+tins?|(?:ice[- ]pop|popsicle|silicone)\s+(?:molds?|moulds?))$",
        r"^(?:(?:latex|rubber|heat[- ]resistant|chemical[- ]resistant)\s+)?gloves?$",
        # Smoking fuel is a material rather than an edible ingredient.
        r"^(?:applewood|wood\s+chips?|smoking\s+wood|cooking\s+wood)$",
    )
)


def _normalize(value: str) -> str:
    text = value.casefold().replace("’", "'").replace("–", "-").replace("—", "-")
    text = _PAREN_RE.sub(" ", text)
    text = re.sub(r"\[[^]]*]\([^)]*\)", " ", text)
    text = text.strip(" \t\r\n-*+.,;:")
    return _SPACE_RE.sub(" ", text)


def is_nonfood_ingredient(raw_text: str, normalized_name: str | None = None) -> bool:
    """Return whether a line is clearly equipment or non-edible material.

    ``normalized_name`` should be the ingredient parser's cleaned name when it
    is available.  Supplying it keeps quantities, units, dimensions, and prep
    comments from weakening an otherwise exact classification.
    """

    raw = _normalize(raw_text)
    if _PACKAGED_FOOD_RE.search(raw):
        return False

    candidates = [raw]
    if normalized_name:
        candidates.insert(0, _normalize(normalized_name))
    # The parser retains trailing dimensions on lines like "6 baking paper
    # square 7x7cm".  Paper rules intentionally accept that tail.
    return any(
        pattern.fullmatch(candidate)
        for candidate in candidates
        for pattern in _CLEAR_NONFOOD_PATTERNS
    )
