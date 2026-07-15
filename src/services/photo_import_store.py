"""Strict local store for the photo-import idempotency ledger."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from models.photo_import import PHOTO_IMPORT_SCHEMA_VERSION, PhotoImportRecord
from models.pantry import CustomPantryItem
from models.purchase_log import PurchaseRecord
from services.profile_store import default_profile_dir

PHOTO_IMPORTS_FILENAME = "photo_imports.json"
IMPORTED_IMAGES_DIRNAME = "imported_images"


@dataclass
class PhotoImportLoadResult:
    records: list[PhotoImportRecord] = field(default_factory=list)
    load_error: str | None = None


class PhotoImportStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else default_profile_dir()
        self.path = self.base_dir / PHOTO_IMPORTS_FILENAME
        self.images_dir = self.base_dir / IMPORTED_IMAGES_DIRNAME

    def load(self) -> PhotoImportLoadResult:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return PhotoImportLoadResult()
        except (json.JSONDecodeError, OSError) as exc:
            return PhotoImportLoadResult(load_error=f"unreadable photo import ledger: {exc}")
        try:
            if int(data.get("version", 0)) != PHOTO_IMPORT_SCHEMA_VERSION:
                raise ValueError(f"unknown version {data.get('version')!r}")
            records = [PhotoImportRecord.from_dict(raw) for raw in data.get("records", [])]
            if len({record.operation_id for record in records}) != len(records):
                raise ValueError("duplicate operation id")
            return PhotoImportLoadResult(records=records)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return PhotoImportLoadResult(load_error=f"malformed photo import ledger: {exc}")

    def to_json_text(self, records: list[PhotoImportRecord]) -> str:
        return json.dumps({
            "version": PHOTO_IMPORT_SCHEMA_VERSION,
            "records": [record.to_dict() for record in records],
        }, indent=2)

    def save(self, records: list[PhotoImportRecord]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.to_json_text(records))
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        if self.images_dir.is_dir():
            for path in self.images_dir.iterdir():
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        pass


def sweep_orphan_imported_images(
    store: PhotoImportStore,
    ledger: list[PhotoImportRecord],
    purchases: list[PurchaseRecord],
    custom_items: list[CustomPantryItem],
) -> None:
    """Remove only unreferenced files inside the imported-image directory."""

    if not store.images_dir.is_dir():
        return
    relative_paths = {
        image.local_path for record in ledger for image in record.images
    }
    relative_paths.update(
        record.photo_path for record in purchases if record.photo_path
    )
    relative_paths.update(
        item.image_path for item in custom_items if item.image_path
    )
    referenced = {
        (store.base_dir / relative).resolve()
        for relative in relative_paths
        if relative
    }
    root = store.images_dir.resolve()
    for path in store.images_dir.iterdir():
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved.parent != root:
            continue
        if not path.name.startswith(".tmp-") and resolved in referenced:
            continue
        try:
            path.unlink()
        except OSError:
            pass
