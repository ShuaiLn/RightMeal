"""Local JSON persistence for the pantry.

Mirrors PlanStore: local only, atomic writes. Unlike the plan, an unreadable
pantry file loads as an *empty pantry*, never None — the pantry always exists.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from models.food import Food
from models.pantry import Pantry
from services.profile_store import default_profile_dir

PANTRY_FILENAME = "pantry.json"


class PantryStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else default_profile_dir()
        self.path = self.base_dir / PANTRY_FILENAME

    def load(self, foods_by_id: dict[str, Food]) -> Pantry:
        """Return the saved pantry, or an empty one on first run / unreadable file."""
        try:
            with self.path.open(encoding="utf-8") as f:
                return Pantry.from_dict(json.load(f), foods_by_id)
        except FileNotFoundError:
            return Pantry()
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
            return Pantry()

    def to_json_text(self, pantry: Pantry) -> str:
        """Serialized file content, for transactional multi-file writes."""
        return json.dumps(pantry.to_dict(), indent=2)

    def save(self, pantry: Pantry) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.to_json_text(pantry))
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
