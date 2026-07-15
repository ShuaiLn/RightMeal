"""Local JSON persistence for generated cooking steps.

Mirrors PantryStore: local only, atomic writes. The cache is derived data —
an unreadable file loads as an empty cache, never an error (steps simply
regenerate on next open).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from services.profile_store import default_profile_dir

RECIPES_FILENAME = "recipes.json"
RECIPES_STORE_VERSION = 1


class RecipeStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else default_profile_dir()
        self.path = self.base_dir / RECIPES_FILENAME

    def load(self) -> dict[str, list[str]]:
        """cache_key -> steps; empty on first run / unreadable file."""
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
            recipes = data.get("recipes", {})
            return {
                str(key): [str(step) for step in steps]
                for key, steps in recipes.items()
                if isinstance(steps, list) and steps
            }
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError, AttributeError):
            return {}

    def to_json_text(self, recipes: dict[str, list[str]]) -> str:
        """Serialized file content, for transactional multi-file writes."""
        return json.dumps(
            {"version": RECIPES_STORE_VERSION, "recipes": recipes}, indent=2
        )

    def save(self, recipes: dict[str, list[str]]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.to_json_text(recipes))
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
