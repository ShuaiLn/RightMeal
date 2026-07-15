"""Deterministic recipe classification from tags, title, and ingredients.

recipe_type gates meal-slot eligibility: sauce / seasoning / drink / dessert /
base can never fill Breakfast, Lunch, or Dinner. A per-recipe entry in
recipe_overrides.json is the final word for anything the rules get wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# recipe_type -> which meal slots this recipe may fill as the primary dish.
MEAL_TYPES_BY_RECIPE_TYPE = {
    "main_meal": ("lunch", "dinner"),
    "breakfast": ("breakfast",),
    "side": (),
    "snack": (),
    "dessert": (),
    "drink": (),
    "sauce": (),
    "seasoning": (),
    "base": (),
}

_DESSERT_TAGS = {"dessert", "cookies", "cookie", "cake", "pie", "pudding", "pastry",
                 "candy", "ice cream", "frosting", "tart", "brownie", "muffin"}
_DRINK_TAGS = {"drink", "tea", "coffee", "cocktail", "smoothie", "juice", "beverage", "shake"}
_SAUCE_TAGS = {"sauce", "syrup", "spread", "dip", "dressing", "jam", "marmalade", "condiment", "gravy"}
_SEASONING_TAGS = {"seasoning", "spice", "rub", "marinade", "spice-mix", "spice mix"}
_BREAD_TAGS = {"bread", "dough", "loaf"}
_BREAKFAST_TAGS = {"breakfast", "pancake", "waffle", "brunch"}
_SIDE_TAGS = {"side", "side-dish", "side dish"}
_SNACK_TAGS = {"snack", "appetizer", "starter"}

_CUISINES = {
    "italian", "mexican", "american", "french", "indian", "chinese", "japanese",
    "thai", "spanish", "greek", "mediterranean", "russian", "german", "korean",
    "vietnamese", "turkish", "english", "irish", "portuguese", "moroccan",
    "lebanese", "swiss", "belgian", "polish", "hungarian", "asian", "caribbean",
    "brazilian", "peruvian", "cuban", "ethiopian", "british", "scottish",
}

_PROTEIN_TAGS = {"beef", "chicken", "pork", "fish", "seafood", "lamb", "turkey",
                 "shrimp", "salmon", "tuna", "bacon", "sausage", "ham", "duck"}

_DISH_CATEGORY_KEYWORDS = [
    ("soup", ("soup", "chowder", "bisque", "broth")),
    ("stew", ("stew", "goulash", "casserole", "braise", "hotpot", "hot pot")),
    ("curry", ("curry", "masala", "korma", "tikka")),
    ("pasta", ("pasta", "spaghetti", "lasagna", "macaroni", "noodle", "penne",
               "fettuccine", "carbonara", "linguine", "ravioli", "gnocchi")),
    ("salad", ("salad", "slaw", "coleslaw")),
    ("sandwich", ("sandwich", "wrap", "burger", "sub", "panini", "toast", "melt")),
    ("taco", ("taco", "burrito", "quesadilla", "enchilada", "fajita")),
    ("stir_fry", ("stir fry", "stir-fry", "stirfry", "fried rice")),
    ("pizza", ("pizza", "flatbread", "focaccia")),
    ("roast", ("roast", "roasted")),
    ("bake", ("baked", "bake", "gratin", "quiche", "frittata")),
    ("bowl", ("bowl", "rice bowl", "grain bowl")),
    ("pie", ("pie", "pot pie")),
    ("omelette", ("omelette", "omelet", "scramble")),
]

_COOKING_METHODS = [
    ("baking", ("bake", "baked", "oven", "roast")),
    ("frying", ("fry", "fried", "saute", "sauté", "pan-fry", "deep-fry")),
    ("boiling", ("boil", "boiled", "simmer", "poach")),
    ("grilling", ("grill", "grilled", "barbecue", "bbq")),
    ("steaming", ("steam", "steamed")),
    ("stir_frying", ("stir fry", "stir-fry", "wok")),
    ("no_cook", ("no-cook", "no cook", "raw", "chill", "refrigerate")),
]


@dataclass
class Classification:
    recipe_type: str
    meal_types: tuple[str, ...]
    cuisine: str
    dish_category: str
    cooking_methods: tuple[str, ...] = field(default_factory=tuple)


def _text_blob(title: str, tags: tuple[str, ...], directions: tuple[str, ...]) -> str:
    return " ".join([title, " ".join(tags), " ".join(directions)]).lower()


def _pick_recipe_type(title: str, tags: tuple[str, ...], has_protein: bool,
                      has_main_carb: bool, has_vegetable: bool) -> str:
    tagset = set(tags)
    tl = title.lower()

    if tagset & _DRINK_TAGS or any(w in tl for w in ("tea", "cocktail", "smoothie", "latte")):
        return "drink"
    if tagset & _SEASONING_TAGS:
        return "seasoning"
    if tagset & _SAUCE_TAGS or tl.endswith("sauce") or tl.endswith("syrup") or tl.endswith("dressing"):
        return "sauce"
    if tagset & _DESSERT_TAGS or any(w in tl for w in ("cake", "cookie", "pie", "pudding", "brownie")):
        # A "sweet" tag alone (e.g. sweet breakfast) is not enough to be dessert.
        if not (tagset & _BREAKFAST_TAGS):
            return "dessert"
    if tagset & _BREAKFAST_TAGS:
        return "breakfast"
    if tagset & _BREAD_TAGS and not has_protein:
        return "base"
    if tagset & _SNACK_TAGS and not has_protein:
        return "snack"
    if tagset & _SIDE_TAGS and not has_protein:
        return "side"

    # Substantial dish => main meal. Requires a protein or a real carb+veg base.
    if has_protein or (has_main_carb and has_vegetable):
        return "main_meal"
    if has_main_carb or has_vegetable:
        return "side"
    return "base"


def _pick_cuisine(tags: tuple[str, ...]) -> str:
    for tag in tags:
        if tag in _CUISINES:
            return tag
    return "international"


def _pick_dish_category(blob: str, recipe_type: str) -> str:
    for category, keywords in _DISH_CATEGORY_KEYWORDS:
        if any(k in blob for k in keywords):
            return category
    if recipe_type == "breakfast":
        return "breakfast_dish"
    return "plate"


def _pick_cooking_methods(blob: str) -> tuple[str, ...]:
    found = [m for m, kws in _COOKING_METHODS if any(k in blob for k in kws)]
    return tuple(found)


def classify(
    title: str,
    tags: tuple[str, ...],
    directions: tuple[str, ...],
    *,
    has_protein: bool,
    has_main_carb: bool,
    has_vegetable: bool,
    override: dict | None = None,
) -> Classification:
    blob = _text_blob(title, tags, directions)
    recipe_type = _pick_recipe_type(title, tags, has_protein, has_main_carb, has_vegetable)
    cuisine = _pick_cuisine(tags)
    dish_category = _pick_dish_category(blob, recipe_type)
    methods = _pick_cooking_methods(blob)

    if override:
        recipe_type = override.get("recipe_type", recipe_type)
        cuisine = override.get("cuisine", cuisine)
        dish_category = override.get("dish_category", dish_category)
        if "cooking_methods" in override:
            methods = tuple(override["cooking_methods"])

    meal_types = MEAL_TYPES_BY_RECIPE_TYPE.get(recipe_type, ())
    if override and "meal_types" in override:
        meal_types = tuple(override["meal_types"])

    return Classification(
        recipe_type=recipe_type,
        meal_types=meal_types,
        cuisine=cuisine,
        dish_category=dish_category,
        cooking_methods=methods,
    )
