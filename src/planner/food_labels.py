"""Display helpers for seed foods: cooked-weight factors and short names.

These two small tables outlived the retired template scheduler. They are used
only for presentation — ``COOKED_YIELD_FACTORS`` turns a dry purchased weight
into a plate weight for labels (nutrition and conservation always stay on the
purchased/dry basis), and ``SHORT_NAMES`` gives a compact word for a food in
portion and pantry labels. Neither affects planning or nutrition.
"""

from __future__ import annotations

# Cooked-weight multipliers for the dry seed foods (display only — nutrients
# and conservation stay on the purchased/dry basis).
COOKED_YIELD_FACTORS: dict[str, float] = {
    "brown_rice": 2.5,
    "rice_white": 2.8,
    "rolled_oats": 2.5,
    "spaghetti_dry": 2.4,
    "black_beans_dry": 2.4,
    "lentils_dry": 2.5,
    "quinoa_dry": 2.85,
}

# Short display words for foods, used in portion and pantry labels.
SHORT_NAMES: dict[str, str] = {
    "brown_rice": "brown rice",
    "rice_white": "rice",
    "rolled_oats": "oats",
    "bread_whole_wheat": "whole-wheat bread",
    "spaghetti_dry": "spaghetti",
    "tortillas_flour": "tortillas",
    "potatoes_russet": "potatoes",
    "eggs_large": "eggs",
    "chicken_breast": "chicken",
    "chicken_thighs": "chicken thighs",
    "ground_beef": "beef",
    "ground_turkey": "turkey",
    "pork_chops": "pork",
    "beef_sirloin_steak": "steak",
    "salmon_fillet": "salmon",
    "canned_salmon": "canned salmon",
    "shrimp": "shrimp",
    "tilapia_fillet": "tilapia",
    "deli_turkey_breast": "deli turkey",
    "canned_tuna": "tuna",
    "black_beans_dry": "black beans",
    "lentils_dry": "lentils",
    "chickpeas_canned": "chickpeas",
    "peanut_butter": "peanut butter",
    "tofu_firm": "tofu",
    "carrots": "carrots",
    "onions_yellow": "onions",
    "cabbage_green": "cabbage",
    "spinach_fresh": "spinach",
    "broccoli_frozen": "broccoli",
    "mixed_veg_frozen": "mixed veg",
    "tomatoes_canned": "tomatoes",
    "bananas": "banana",
    "apples_gala": "apple",
    "oranges_navel": "orange",
    "berries_frozen": "berries",
    "raisins": "raisins",
    "milk_whole": "milk",
    "yogurt_plain": "yogurt",
    "cheddar_cheese": "cheddar",
    "soy_milk_fortified": "soy milk",
    "canola_oil": "canola oil",
    "olive_oil": "olive oil",
    "avocados": "avocado",
    "bell_peppers": "bell pepper",
    "peas_frozen": "peas",
    "sweet_potatoes": "sweet potato",
    "grapes": "grapes",
    "peaches_canned": "peaches",
    "cottage_cheese": "cottage cheese",
    "almonds": "almonds",
    "quinoa_dry": "quinoa",
    "tortillas_corn": "corn tortillas",
}
