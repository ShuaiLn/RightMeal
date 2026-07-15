"""Parse one free-text ingredient line into structured parts.

Handles the messy reality of the corpus, e.g.:

    "1/2 lb Beef, cut into strips"      -> qty 0.5, unit lb,  name "beef", prep "cut into strips"
    "1 cup flour (2.5 dl)"              -> qty 1,   unit cup, name "flour"
    "226 g (2 sticks) cold butter"      -> grams 226,          name "butter", prep "cold"
    "2 large yellow onions, finely diced" -> qty 2, name "yellow onions", prep "finely diced"
    "Salt to taste"                     -> qty None, name "salt", prep "to taste"
    "¼ cup chopped fresh basil"         -> qty 0.25, unit cup, name "basil", state raw/fresh

The parser does NOT convert to grams — that needs the ingredient portion
defaults (density / count weights) applied downstream. It only surfaces an
explicit mass/volume when the line literally states one (``grams_explicit`` /
``ml_explicit``), which the resolver/nutrition step prefers over unit math.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_UNICODE_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75,
    "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8, "⅙": 1 / 6, "⅚": 5 / 6,
    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

_MASS_TO_G = {
    "g": 1.0, "gram": 1.0, "grams": 1.0, "gr": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
}

_VOLUME_TO_ML = {
    "ml": 1.0, "milliliter": 1.0, "milliliters": 1.0,
    "l": 1000.0, "liter": 1000.0, "liters": 1000.0, "litre": 1000.0, "litres": 1000.0,
    "dl": 100.0, "cl": 10.0,
    "tsp": 4.92892, "teaspoon": 4.92892, "teaspoons": 4.92892,
    "tbsp": 14.7868, "tablespoon": 14.7868, "tablespoons": 14.7868, "tbs": 14.7868,
    "cup": 236.588, "cups": 236.588,
    "pint": 473.176, "pints": 473.176, "quart": 946.353, "quarts": 946.353,
    "gallon": 3785.41, "gallons": 3785.41,
    "fl oz": 29.5735, "floz": 29.5735,
}

# Count / descriptive units — kept as the unit; grams come from portion defaults.
_COUNT_UNITS = {
    "clove", "cloves", "can", "cans", "stick", "sticks", "slice", "slices",
    "piece", "pieces", "bunch", "bunches", "head", "heads", "handful", "handfuls",
    "pinch", "pinches", "dash", "dashes", "package", "packages", "packet", "packets",
    "sprig", "sprigs", "leaf", "leaves", "fillet", "fillets", "breast", "breasts",
    "thigh", "thighs", "sheet", "sheets", "loaf", "loaves", "jar", "jars", "tin", "tins",
    "block", "blocks", "bag", "bags", "bar", "bars", "cube", "cubes", "strip", "strips",
    "wedge", "wedges", "ball", "balls", "ear", "ears", "stalk", "stalks", "rib", "ribs",
}

_ALL_UNITS = set(_MASS_TO_G) | set(_VOLUME_TO_ML) | _COUNT_UNITS

# Prep / preparation-state vocabulary. State is the physically important part;
# nutrition must never compute a cooked quantity with dry-food nutrients.
_STATE_WORDS = {
    "cooked": "cooked", "boiled": "cooked", "steamed": "cooked", "roasted": "cooked",
    "baked": "cooked", "grilled": "cooked", "fried": "cooked", "leftover": "cooked",
    "dry": "dry", "dried": "dry", "uncooked": "dry",
    "drained": "drained", "rinsed and drained": "drained",
    "canned": "canned", "tinned": "canned",
    "raw": "raw", "fresh": "raw", "frozen": "raw",
}

_OPTIONAL_RE = re.compile(r"\boptional\b|\bto taste\b|\bfor garnish\b|\bfor serving\b|\bif desired\b", re.I)
_PLUS_RE = re.compile(r"\bfor (?:frying|greasing|brushing|dusting|the pan|drizzling)\b", re.I)
_LEADING_ARTICLE_RE = re.compile(r"^(?:a|an|some|of|the)\s+", re.I)
_PAREN_RE = re.compile(r"\([^)]*\)")


def _num(token: str) -> float | None:
    """Parse a numeric token: '1', '1/2', '1.5', '½', '1½', '1 1/2' handled upstream."""
    token = token.strip()
    if not token:
        return None
    total = 0.0
    consumed = False
    # unicode fraction possibly attached to an integer (e.g. "1½")
    lead = ""
    for ch in token:
        if ch in _UNICODE_FRACTIONS:
            total += _UNICODE_FRACTIONS[ch]
            consumed = True
        else:
            lead += ch
    lead = lead.strip()
    if lead:
        if "/" in lead:
            try:
                a, b = lead.split("/", 1)
                total += float(a) / float(b)
                consumed = True
            except (ValueError, ZeroDivisionError):
                return None
        else:
            try:
                total += float(lead)
                consumed = True
            except ValueError:
                return None
    return total if consumed else None


def _parse_leading_quantity(text: str) -> tuple[float | None, str]:
    """Pull a leading quantity (incl. ranges '1-2' and mixed '1 1/2') off text.

    Returns (quantity or None, remaining text). For a range, the midpoint is
    used so downstream sizing is stable and deterministic.
    """
    s = text.strip()
    # Range like "1-2" / "50-100" / "1 to 2": take the midpoint.
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\b", s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo + hi) / 2.0, s[m.end():].strip()
    # Mixed number "1 1/2" or "1 ½".
    m = re.match(r"^(\d+)\s+(\d+/\d+|[" + "".join(_UNICODE_FRACTIONS) + r"])\b", s)
    if m:
        whole = float(m.group(1))
        frac = _num(m.group(2)) or 0.0
        return whole + frac, s[m.end():].strip()
    # Simple fraction, decimal, integer, or a leading unicode fraction.
    m = re.match(r"^(\d+/\d+|\d+(?:\.\d+)?|[" + "".join(_UNICODE_FRACTIONS) + r"]+)\b", s)
    if m:
        return _num(m.group(1)), s[m.end():].strip()
    return None, s


@dataclass
class ParsedLine:
    raw_text: str
    quantity: float | None
    unit: str | None          # normalized unit token, or None for "2 eggs"
    name: str                 # cleaned ingredient name for resolution
    prep_notes: str           # what came after the comma / descriptive words
    state_hint: str | None    # raw | dry | cooked | drained | canned
    grams_explicit: float | None  # set when the line literally states a mass
    ml_explicit: float | None     # set when the line literally states a volume
    optional: bool


def _paren_metric(text: str) -> tuple[float | None, float | None]:
    """First mass/volume measure found *inside parentheses* (a metric hint for
    one leading unit, e.g. '(2.5 dl)', '(28 oz.)'). Prefers grams. Range = mid."""
    for paren in re.findall(r"\(([^)]*)\)", text):
        for m in re.finditer(
            r"(\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*"
            r"(kg|g|gram|grams|oz|ounce|ounces|lb|lbs|pound|pounds|ml|l|dl|cl|litre|litres|liter|liters)\b",
            paren, re.I,
        ):
            lo = float(m.group(1))
            val = (lo + float(m.group(2))) / 2.0 if m.group(2) else lo
            unit = m.group(3).lower()
            if unit in _MASS_TO_G:
                return val * _MASS_TO_G[unit], None
            if unit in _VOLUME_TO_ML:
                return None, val * _VOLUME_TO_ML[unit]
    return None, None


_LEADING_DESCRIPTOR_RE = re.compile(
    r"^\s*(?:large|small|medium|whole|ripe|very ripe|fresh|dried|ground|boneless|"
    r"skinless|cold|warm|hot|extra|thinly|thickly|finely|coarsely|roughly|"
    r"chopped|minced|diced|sliced|grated|shredded|crushed|mashed|cooked|raw|"
    r"lean|firm|soft|good|quality|plain|unsalted|salted|sweet|smooth|creamy)\s+",
    re.I,
)


def parse_ingredient_line(text: str) -> ParsedLine:
    raw = text.strip()
    # Normalize the Unicode fraction slash (U+2044) / division slash (U+2215)
    # so "3⁄4 rolled oats" parses like "3/4 rolled oats".
    text = raw.replace("⁄", "/").replace("∕", "/")
    optional = bool(_OPTIONAL_RE.search(raw)) or bool(_PLUS_RE.search(raw))

    # Split trailing prep clause on the first comma (keep for prep_notes).
    head, _, tail = text.partition(",")
    prep_notes = tail.strip()

    # Remove parentheticals from the working name (they held metric hints).
    working = _PAREN_RE.sub(" ", head).strip()

    # Leading quantity, then optional unit.
    qty, rest = _parse_leading_quantity(working)
    unit: str | None = None
    tokens = rest.split()
    if tokens:
        first = tokens[0].lower().strip(".")
        # "fl oz" is two tokens.
        two = (first + " " + tokens[1].lower().strip(".")) if len(tokens) > 1 else ""
        if two in _ALL_UNITS:
            unit = two
            rest = " ".join(tokens[2:])
        elif first in _ALL_UNITS:
            unit = first
            rest = " ".join(tokens[1:])

    # Explicit mass/volume derives from the parsed leading quantity+unit (never
    # a raw-line number scan, which would trip over fraction denominators like
    # the "2" in "1/2 lb"). A parenthetical metric refines a count/unitless
    # line as the size of one leading unit ("2 cans (28 oz)" -> 2 x 28 oz).
    grams_explicit: float | None = None
    ml_explicit: float | None = None
    if qty is not None and unit in _MASS_TO_G:
        grams_explicit = qty * _MASS_TO_G[unit]
    elif qty is not None and unit in _VOLUME_TO_ML:
        ml_explicit = qty * _VOLUME_TO_ML[unit]
    else:
        p_g, p_ml = _paren_metric(raw)
        mult = qty if qty is not None else 1.0
        if p_g is not None:
            grams_explicit = mult * p_g
        elif p_ml is not None:
            ml_explicit = mult * p_ml

    # Strip stacked leading descriptors ("chopped fresh basil" -> "basil").
    prev = None
    while prev != rest:
        prev = rest
        rest = _LEADING_DESCRIPTOR_RE.sub("", rest)

    # Pull state hint from the full line.
    state_hint: str | None = None
    low = raw.lower()
    for word, state in _STATE_WORDS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", low):
            state_hint = state
            break

    name = _LEADING_ARTICLE_RE.sub("", rest).strip()
    # Trailing descriptive words that aren't part of the core noun.
    name = re.sub(r"\s+(?:cut into.*|minced|chopped|diced|sliced|grated|shredded|"
                  r"peeled|crushed|melted|softened|beaten|to taste|for .*)$", "", name, flags=re.I).strip()
    name = re.sub(r"\s{2,}", " ", name).strip(" .,-")

    if not prep_notes:
        # Recover a prep note from descriptive words even without a comma.
        m = re.search(r"\b(minced|chopped|diced|sliced|grated|shredded|crushed|melted|"
                      r"softened|beaten|cut into [\w\s]+)\b", low)
        if m:
            prep_notes = m.group(1)

    return ParsedLine(
        raw_text=raw,
        quantity=qty,
        unit=unit,
        name=name.lower(),
        prep_notes=prep_notes,
        state_hint=state_hint,
        grams_explicit=grams_explicit,
        ml_explicit=ml_explicit,
        optional=optional,
    )
