"""Dev-time recipe catalog compiler.

Parses the read-only public-domain recipes in ``content/`` into a normalized,
cached ``src/data/recipe_index.json`` that the app loads at runtime. The
original markdown is never modified; this package only reads it.

Pipeline (see ``build_recipe_index.py``):
    md_parser  -> raw recipe structure (frontmatter, ingredients, directions)
    ingredient_parser -> quantity / unit / name / prep / state per line
    resolver   -> canonical food id via alias/override hierarchy
    roles      -> ingredient roles + core-role detection + rice variants
    classifier -> recipe_type / meal_types / cuisine / dish_category
    batch      -> Recipe-level batch fields (with category fallback)
    nutrition  -> per-serving nutrition by nutrition_basis + coverage + gate

No app imports here: these modules run under plain ``python`` at build time.
"""
