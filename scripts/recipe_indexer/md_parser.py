"""Parse a public-domain-recipes markdown file into a raw structure.

The files follow the based.cooking / public-domain-recipes layout:

    ---
    title: "Beef and Broccoli"
    tags: ['beef', 'asian', 'rice']
    date: 2022-09-10
    author: joel-maxuel
    ---

    - ⏲️ Prep time: 15 min
    - 🍳 Cook time: 20 min
    - 🍽️ Servings: 3

    ## Ingredients

    - 1/2 lb Beef, cut into strips
    - ...

    ## Directions

    1. ...

Some files also embed an image ``![alt](/pix/<slug>.webp)``.

The project has no YAML dependency, so the frontmatter is parsed by hand; only
the handful of scalar keys we need are understood. ``_index.md`` is not a
recipe and must be skipped by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<url>[^)\s]+)")
# Capture the heading level: only level-2 (##) headings switch the top-level
# section; deeper (### Filling / ### Crust) are subsections that keep grouping
# ingredients or directions under the current section.
_HEADING_RE = re.compile(r"^\s{0,3}(#{2,6})\s+(.*?)\s*#*\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_TIME_RE = re.compile(r"(\d+)\s*(?:h|hr|hour|hours)?\s*(\d+)?\s*(?:m|min|minute|minutes)?", re.I)

# Emoji / label markers for the metadata bullet lines. We match on the label
# text rather than the emoji so odd encodings still resolve.
_PREP_RE = re.compile(r"prep\s*time", re.I)
_COOK_RE = re.compile(r"cook\s*time", re.I)
_SERVINGS_RE = re.compile(r"serv(?:es|ings?)", re.I)


@dataclass
class RawRecipe:
    """The unnormalized content of one recipe markdown file."""

    slug: str
    source_file: str  # repo-relative, forward-slashed (e.g. "content/apple-pie.md")
    title: str
    tags: tuple[str, ...] = ()
    author: str = ""
    date: str = ""
    servings: int | None = None
    prep_time_min: int | None = None
    cook_time_min: int | None = None
    image_slug: str | None = None  # e.g. "apple-pie" from /pix/apple-pie.webp
    raw_ingredients: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()
    body_meta: dict[str, str] = field(default_factory=dict)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1].strip()
    return value


def _parse_tag_list(value: str) -> tuple[str, ...]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    tags: list[str] = []
    for part in value.split(","):
        tag = _strip_quotes(part).lower()
        if tag:
            tags.append(tag)
    return tuple(tags)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body) — body is everything after the block."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    block = match.group(1)
    body = text[match.end():]
    fm: dict = {}
    key: str | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        # Only top-level "key: value" pairs matter here.
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m:
            key = m.group(1).lower()
            fm[key] = m.group(2).strip()
    return fm, body


def _parse_time_to_minutes(value: str) -> int | None:
    value = value.strip().lower()
    if not value:
        return None
    total = 0
    matched = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", value):
        matched = True
        n = float(num)
        if unit.startswith("h"):
            total += int(round(n * 60))
        else:
            total += int(round(n))
    if matched:
        return total or None
    # Bare number: assume minutes.
    m = re.search(r"\d+", value)
    return int(m.group()) if m else None


def _parse_servings(value: str) -> int | None:
    m = re.search(r"\d+", value)
    if not m:
        return None
    n = int(m.group())
    return n if 1 <= n <= 40 else None


def parse_recipe_md(path: Path, project_root: Path) -> RawRecipe:
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)

    slug = path.stem
    source_file = path.relative_to(project_root).as_posix()
    title = _strip_quotes(fm.get("title", "")) or slug.replace("-", " ").title()
    tags = _parse_tag_list(fm.get("tags", "")) if fm.get("tags") else ()
    author = _strip_quotes(fm.get("author", ""))
    date = _strip_quotes(fm.get("date", ""))

    image_slug: str | None = None
    servings: int | None = None
    prep: int | None = None
    cook: int | None = None
    raw_ingredients: list[str] = []
    directions: list[str] = []

    img = _IMAGE_RE.search(body)
    if img:
        url = img.group("url")
        m = re.search(r"/pix/([^/]+?)\.(?:webp|jpg|jpeg|png)$", url, re.I)
        if m:
            image_slug = m.group(1)

    section: str | None = None  # None -> preamble, "ingredients", "directions", "other"
    for line in body.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            label = heading.group(2).strip().lower()
            if level == 2:
                if "ingredient" in label:
                    section = "ingredients"
                elif (
                    "direction" in label or "method" in label
                    or "instruction" in label or "step" in label
                ):
                    section = "directions"
                else:
                    section = "other"
            # Deeper (### ...) headings are subsections: keep the current
            # section so grouped ingredients/steps are still collected.
            continue

        item = _LIST_ITEM_RE.match(line)
        content = item.group(1).strip() if item else line.strip()

        if section is None or section == "other":
            # Metadata bullets live in the preamble (and occasionally elsewhere).
            if item:
                if _PREP_RE.search(content):
                    prep = _parse_time_to_minutes(content.split(":", 1)[-1]) if ":" in content else prep
                elif _COOK_RE.search(content):
                    cook = _parse_time_to_minutes(content.split(":", 1)[-1]) if ":" in content else cook
                elif _SERVINGS_RE.search(content):
                    servings = _parse_servings(content.split(":", 1)[-1]) if ":" in content else servings
            continue

        if section == "ingredients":
            if item and content:
                raw_ingredients.append(content)
        elif section == "directions":
            if content:
                directions.append(content)

    return RawRecipe(
        slug=slug,
        source_file=source_file,
        title=title,
        tags=tags,
        author=author,
        date=date,
        servings=servings,
        prep_time_min=prep,
        cook_time_min=cook,
        image_slug=image_slug,
        raw_ingredients=tuple(raw_ingredients),
        directions=tuple(directions),
    )


def iter_recipe_paths(content_dir: Path):
    """Yield every recipe markdown path except the non-recipe ``_index.md``."""
    for path in sorted(content_dir.glob("*.md")):
        if path.name == "_index.md":
            continue
        yield path
