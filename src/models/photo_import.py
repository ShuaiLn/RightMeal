"""Idempotency and image-lifecycle ledger for confirmed photo imports."""

from __future__ import annotations

from dataclasses import dataclass

from models.photo_analysis import PhotoKind

PHOTO_IMPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ImportedImage:
    sha256: str
    local_path: str
    segment_index: int

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "local_path": self.local_path.replace("\\", "/"),
            "segment_index": self.segment_index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImportedImage":
        if set(data) != {"sha256", "local_path", "segment_index"}:
            raise ValueError("unexpected imported image fields")
        sha256 = str(data["sha256"])
        path = str(data["local_path"]).replace("\\", "/")
        segment = int(data.get("segment_index", 0))
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ValueError("invalid sanitized image hash")
        if (
            not path
            or path.startswith(("/", "\\"))
            or (len(path) > 1 and path[1] == ":")
            or ".." in path.split("/")
        ):
            raise ValueError("photo import image path must be relative")
        if segment < 0:
            raise ValueError("segment index must be non-negative")
        return cls(sha256=sha256, local_path=path, segment_index=segment)


@dataclass(frozen=True)
class PhotoImportRecord:
    operation_id: str
    photo_kind: PhotoKind
    imported_at: str
    images: tuple[ImportedImage, ...]
    transaction_fingerprint: str | None
    purchase_event_ids: tuple[str, ...]
    custom_pantry_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "photo_kind": self.photo_kind.value,
            "imported_at": self.imported_at,
            "images": [image.to_dict() for image in self.images],
            "transaction_fingerprint": self.transaction_fingerprint,
            "purchase_event_ids": list(self.purchase_event_ids),
            "custom_pantry_ids": list(self.custom_pantry_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PhotoImportRecord":
        expected = {
            "operation_id", "photo_kind", "imported_at", "images",
            "transaction_fingerprint", "purchase_event_ids", "custom_pantry_ids",
        }
        if set(data) != expected:
            raise ValueError("unexpected photo import fields")
        fingerprint = data.get("transaction_fingerprint")
        if fingerprint is not None:
            fingerprint = str(fingerprint)
            if len(fingerprint) != 64 or any(
                char not in "0123456789abcdef" for char in fingerprint
            ):
                raise ValueError("invalid transaction fingerprint")
        operation_id = str(data["operation_id"])
        if not operation_id:
            raise ValueError("operation id is required")
        photo_kind = PhotoKind(data["photo_kind"])
        if photo_kind not in (PhotoKind.PRODUCT, PhotoKind.RECEIPT):
            raise ValueError("only product and receipt imports may be persisted")
        purchase_ids = tuple(str(value) for value in data.get("purchase_event_ids", []))
        custom_ids = tuple(str(value) for value in data.get("custom_pantry_ids", []))
        if any(not value for value in (*purchase_ids, *custom_ids)):
            raise ValueError("committed child IDs must not be empty")
        return cls(
            operation_id=operation_id,
            photo_kind=photo_kind,
            imported_at=str(data["imported_at"]),
            images=tuple(ImportedImage.from_dict(raw) for raw in data.get("images", [])),
            transaction_fingerprint=fingerprint,
            purchase_event_ids=purchase_ids,
            custom_pantry_ids=custom_ids,
        )
