"""Local JSON persistence for the purchase log.

Purchase records are the SOURCE OF TRUTH for purchases — a corrupted file is
never treated as a legal empty log. ``load`` reports the failure instead of
returning empty: the app must then pause purchase mutations, skip aggregate
rebuilds, and skip the photo sweep, leaving the file untouched for recovery.
(Contrast: derived caches like recipes.json may legally load empty.)
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from models.purchase_log import PURCHASE_LOG_SCHEMA_VERSION, PurchaseRecord
from services.profile_store import default_profile_dir

PURCHASES_FILENAME = "purchases.json"
PURCHASE_PHOTOS_DIRNAME = "purchase_photos"


@dataclass
class PurchaseLogLoadResult:
    records: list[PurchaseRecord] = field(default_factory=list)
    load_error: str | None = None


class PurchaseLogStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else default_profile_dir()
        self.path = self.base_dir / PURCHASES_FILENAME
        self.photos_dir = self.base_dir / PURCHASE_PHOTOS_DIRNAME

    def load(self) -> PurchaseLogLoadResult:
        """All records, or a load_error — NEVER a silently-empty log when the
        file exists but can't be read (any malformed record fails the load:
        partial history would corrupt undo baselines and aggregates)."""
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return PurchaseLogLoadResult()
        except (json.JSONDecodeError, OSError) as exc:
            return PurchaseLogLoadResult(load_error=f"unreadable purchase log: {exc}")
        try:
            if int(data.get("version", 0)) not in (1, PURCHASE_LOG_SCHEMA_VERSION):
                return PurchaseLogLoadResult(
                    load_error=f"unknown purchase log version: {data.get('version')!r}"
                )
            records = [PurchaseRecord.from_dict(raw) for raw in data.get("records", [])]
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            return PurchaseLogLoadResult(load_error=f"malformed purchase record: {exc}")
        return PurchaseLogLoadResult(records=records)

    def to_json_text(self, records: list[PurchaseRecord]) -> str:
        """Serialized file content, for transactional multi-file writes."""
        return json.dumps(
            {
                "version": PURCHASE_LOG_SCHEMA_VERSION,
                "records": [record.to_dict() for record in records],
            },
            indent=2,
        )

    def save(self, records: list[PurchaseRecord]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.to_json_text(records))
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
        if self.photos_dir.is_dir():
            for path in self.photos_dir.iterdir():
                try:
                    path.unlink()
                except OSError:
                    pass


def sweep_orphan_photos(store: PurchaseLogStore, records: list[PurchaseRecord]) -> None:
    """Delete .tmp-* leftovers and photos no record references — scoped
    strictly to the purchase_photos directory, and only ever run after the
    log loaded CLEANLY (a failed load must skip the sweep or it would delete
    every referenced photo)."""
    if not store.photos_dir.is_dir():
        return
    referenced = {
        (store.base_dir / record.photo_path).resolve()
        for record in records
        if record.photo_path
    }
    for path in store.photos_dir.iterdir():
        if not path.is_file():
            continue
        if not path.name.startswith(".tmp-") and path.resolve() in referenced:
            continue
        try:
            path.unlink()
        except OSError:
            pass
