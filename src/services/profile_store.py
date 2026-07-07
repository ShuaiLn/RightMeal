"""Local JSON persistence for the household profile.

RightMeal stores profile data locally only — no accounts, no cloud sync.
Deleting the profile file removes all saved data.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from models.profile import HouseholdProfile

PROFILE_FILENAME = "profile.json"


def default_profile_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "RightMeal"
    return Path.home() / ".rightmeal"


class ProfileStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else default_profile_dir()
        self.path = self.base_dir / PROFILE_FILENAME

    def load(self) -> HouseholdProfile | None:
        """Return the saved profile, or None on first run / unreadable file."""
        try:
            with self.path.open(encoding="utf-8") as f:
                return HouseholdProfile.from_dict(json.load(f))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            return None

    def save(self, profile: HouseholdProfile) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(profile.to_dict(), f, indent=2)
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
