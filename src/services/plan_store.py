"""Local JSON persistence for the saved meal plan.

Mirrors ProfileStore: local only, atomic writes, unreadable files load as None.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from models.food import Food
from models.plan import SavedPlan
from services.profile_store import default_profile_dir

PLAN_FILENAME = "plan.json"


class PlanStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else default_profile_dir()
        self.path = self.base_dir / PLAN_FILENAME

    def load(self, foods_by_id: dict[str, Food]) -> SavedPlan | None:
        """Return the saved plan, or None on first run / unreadable file."""
        try:
            with self.path.open(encoding="utf-8") as f:
                return SavedPlan.from_dict(json.load(f), foods_by_id)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
            return None

    def to_json_text(self, plan: SavedPlan) -> str:
        """Serialized file content, for transactional multi-file writes."""
        return json.dumps(plan.to_dict(), indent=2)

    def save(self, plan: SavedPlan) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.to_json_text(plan))
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
