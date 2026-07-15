"""Local JSON persistence for prepared leftovers.

Mirrors PantryStore: local only, atomic writes, an unreadable file loads as an
empty list. Loading re-derives every record's servings from its portions —
the serialized cache is never trusted (see models.prepared_leftover).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from models.food import Food
from models.prepared_leftover import LEFTOVERS_SCHEMA_VERSION, PreparedLeftover
from services.profile_store import default_profile_dir

LEFTOVERS_FILENAME = "leftovers.json"


class PreparedLeftoversStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else default_profile_dir()
        self.path = self.base_dir / LEFTOVERS_FILENAME

    def load(self, foods_by_id: dict[str, Food]) -> list[PreparedLeftover]:
        """Return the saved leftovers, or [] on first run / unreadable file.
        Malformed records are dropped individually."""
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
            # v1 (no component provenance) and v2 both load: from_dict fills the
            # missing component_kind/source_recipe_id with plain-main defaults.
            if data.get("version") not in (1, LEFTOVERS_SCHEMA_VERSION):
                return []
            items = []
            for raw in list(data.get("items", [])):
                leftover = PreparedLeftover.from_dict(raw, foods_by_id)
                if leftover is not None:
                    items.append(leftover)
            return items
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, AttributeError, KeyError, ValueError, TypeError, OSError):
            return []

    def to_json_text(self, items: list[PreparedLeftover]) -> str:
        """Serialized file content, for transactional multi-file writes."""
        return json.dumps(
            {
                "version": LEFTOVERS_SCHEMA_VERSION,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "items": [leftover.to_dict() for leftover in items],
            },
            indent=2,
        )

    def save(self, items: list[PreparedLeftover]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.to_json_text(items))
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
