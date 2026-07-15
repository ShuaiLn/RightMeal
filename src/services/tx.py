"""Multi-file transactional persistence with a rollback journal.

Every store write in the app goes through one shared TransactionManager so a
meal-tracking action that touches plan.json, pantry.json and leftovers.json
either lands entirely or not at all — including across a crash. The journal
holds the pre-transaction text of every file about to change; recovery on the
next launch rolls an interrupted transaction back to that snapshot.

A single process-wide lock serialises writers (Flet handlers are a mix of
sync and async entry points, so this is a threading lock, not an asyncio one).
Cross-process concurrency is out of scope: the app runs as a single process.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

TX_JOURNAL_FILENAME = "tx_journal.json"

# The only files a transaction (and therefore recovery) may ever touch.
# purchases.json and recipes.json are written through AppState.persist too, so
# they must be allowed (a prior gap made those writes raise).
DEFAULT_ALLOWED_FILENAMES = frozenset(
    {
        "plan.json", "pantry.json", "leftovers.json", "purchases.json",
        "recipes.json", "photo_imports.json",
    }
)


def _atomic_write(path: Path, text: str) -> None:
    """temp file + fsync + os.replace, so a crash never leaves a torn file.

    os.replace is atomic on the same volume; the fsync makes the content
    durable before the replace so power loss cannot resurrect stale bytes.
    """
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class TransactionManager:
    def __init__(self, base_dir: Path, allowed_filenames: Iterable[str] | None = None):
        self.base_dir = Path(base_dir)
        self.journal_path = self.base_dir / TX_JOURNAL_FILENAME
        self._allowed = frozenset(allowed_filenames) if allowed_filenames else DEFAULT_ALLOWED_FILENAMES
        self._lock = threading.RLock()

    # -- writing ---------------------------------------------------------

    def save_all(self, writes: Mapping[Path, str]) -> None:
        """Write every file or none: journal the old contents, replace each
        target, then drop the journal (the commit point). Any failure rolls
        the already-replaced files back from the journal; if that rollback
        itself fails the journal is kept for recovery on next launch."""
        if not writes:
            return
        with self._lock:
            named = self._validated(writes)
            self.base_dir.mkdir(parents=True, exist_ok=True)
            journal = {
                "tx_id": uuid.uuid4().hex,
                "files": {name: self._read_or_none(name) for name in named},
            }
            _atomic_write(self.journal_path, json.dumps(journal, indent=2))
            try:
                for name, text in named.items():
                    _atomic_write(self.base_dir / name, text)
            except BaseException:
                self._rollback(journal["files"])  # keeps journal if it fails
                raise
            self.journal_path.unlink()

    def _validated(self, writes: Mapping[Path, str]) -> dict[str, str]:
        named: dict[str, str] = {}
        for path, text in writes.items():
            path = Path(path)
            if path.name not in self._allowed or Path(path).resolve().parent != self.base_dir.resolve():
                raise ValueError(f"transaction refuses to write outside its store set: {path}")
            named[path.name] = text
        return named

    def _read_or_none(self, name: str) -> str | None:
        try:
            return (self.base_dir / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _rollback(self, files: Mapping[str, str | None]) -> None:
        for name, old_text in files.items():
            target = self.base_dir / name
            if old_text is None:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            else:
                _atomic_write(target, old_text)
        self.journal_path.unlink()

    # -- recovery --------------------------------------------------------

    def recover_pending(self) -> str | None:
        """Roll back an interrupted transaction found on disk.

        Returns a user-facing warning string when something happened (rolled
        back, or the journal was unreadable and got quarantined), else None.
        A corrupt journal is never treated as "no transaction" — it is moved
        aside so the files it can no longer describe are left untouched.
        """
        with self._lock:
            if not self.journal_path.exists():
                return None
            try:
                journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
                files = journal["files"]
                if not isinstance(files, dict):
                    raise ValueError("journal 'files' is not a dict")
                safe = {}
                for name, old_text in files.items():
                    name = str(name)
                    if name not in self._allowed:
                        raise ValueError(f"journal names an unknown file: {name}")
                    if old_text is not None and not isinstance(old_text, str):
                        raise ValueError(f"journal content for {name} is not text")
                    safe[name] = old_text
            except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
                quarantine = self.base_dir / (
                    f"tx_journal.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
                )
                try:
                    os.replace(self.journal_path, quarantine)
                except OSError:
                    pass
                return (
                    "A previous save was interrupted and its recovery journal is "
                    "unreadable; it was set aside. Please review your plan and pantry."
                )
            self._rollback(safe)
            return "An interrupted save was rolled back to the last consistent state."
