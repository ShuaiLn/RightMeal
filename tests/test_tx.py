"""Transactional multi-file persistence: all-or-nothing writes and recovery."""

import json
import threading
from pathlib import Path

import pytest

from services import tx as tx_module
from services.tx import (
    TX_JOURNAL_FILENAME,
    TransactionManager,
    TransactionRecoveryRequiredError,
    TransactionStatus,
)


def read(base_dir, name):
    return (base_dir / name).read_text(encoding="utf-8")


def seed(base_dir, contents: dict[str, str]) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    for name, text in contents.items():
        (base_dir / name).write_text(text, encoding="utf-8")


class TestSaveAll:
    def test_success_writes_everything_and_leaves_no_journal(self, tmp_path):
        seed(tmp_path, {"plan.json": "old-plan"})
        manager = TransactionManager(tmp_path)
        manager.save_all({
            tmp_path / "plan.json": "new-plan",
            tmp_path / "pantry.json": "new-pantry",
        })
        assert read(tmp_path, "plan.json") == "new-plan"
        assert read(tmp_path, "pantry.json") == "new-pantry"
        assert not (tmp_path / TX_JOURNAL_FILENAME).exists()

    def test_empty_writes_is_a_noop(self, tmp_path):
        TransactionManager(tmp_path).save_all({})
        assert not (tmp_path / TX_JOURNAL_FILENAME).exists()

    def test_rejects_paths_outside_base_dir(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        manager = TransactionManager(tmp_path / "store")
        with pytest.raises(ValueError):
            manager.save_all({elsewhere / "plan.json": "x"})
        assert not (elsewhere / "plan.json").exists()

    def test_rejects_unknown_filenames(self, tmp_path):
        manager = TransactionManager(tmp_path)
        with pytest.raises(ValueError):
            manager.save_all({tmp_path / "passwords.txt": "x"})
        assert not (tmp_path / "passwords.txt").exists()

    def test_midway_failure_rolls_back_written_files(self, tmp_path, monkeypatch):
        seed(tmp_path, {"plan.json": "old-plan", "pantry.json": "old-pantry"})
        manager = TransactionManager(tmp_path)
        real_write = tx_module._atomic_write

        def failing_write(path, text):
            if path.name == "pantry.json" and text == "new-pantry":
                raise OSError("disk full")
            real_write(path, text)

        monkeypatch.setattr(tx_module, "_atomic_write", failing_write)
        with pytest.raises(OSError):
            manager.save_all({
                tmp_path / "plan.json": "new-plan",
                tmp_path / "pantry.json": "new-pantry",
            })
        # plan.json had already been replaced; the rollback restored it.
        assert read(tmp_path, "plan.json") == "old-plan"
        assert read(tmp_path, "pantry.json") == "old-pantry"
        assert not (tmp_path / TX_JOURNAL_FILENAME).exists()

    def test_rollback_deletes_files_that_did_not_exist_before(self, tmp_path, monkeypatch):
        seed(tmp_path, {"pantry.json": "old-pantry"})
        manager = TransactionManager(tmp_path)
        real_write = tx_module._atomic_write

        def failing_write(path, text):
            if path.name == "pantry.json" and text == "new-pantry":
                raise OSError("disk full")
            real_write(path, text)

        monkeypatch.setattr(tx_module, "_atomic_write", failing_write)
        with pytest.raises(OSError):
            manager.save_all({
                tmp_path / "leftovers.json": "new-leftovers",  # created by this tx
                tmp_path / "pantry.json": "new-pantry",
            })
        assert not (tmp_path / "leftovers.json").exists()
        assert read(tmp_path, "pantry.json") == "old-pantry"

    def test_failed_rollback_keeps_journal_for_recovery(self, tmp_path, monkeypatch):
        seed(tmp_path, {"plan.json": "old-plan", "pantry.json": "old-pantry"})
        manager = TransactionManager(tmp_path)
        real_write = tx_module._atomic_write
        calls = {"failing": False}

        def failing_write(path, text):
            if path.name == "pantry.json" and text == "new-pantry":
                calls["failing"] = True  # from here on, everything fails (dead disk)
            if calls["failing"]:
                raise OSError("disk full")
            real_write(path, text)

        monkeypatch.setattr(tx_module, "_atomic_write", failing_write)
        with pytest.raises(OSError):
            manager.save_all({
                tmp_path / "plan.json": "new-plan",
                tmp_path / "pantry.json": "new-pantry",
            })
        assert (tmp_path / TX_JOURNAL_FILENAME).exists()
        # Next launch, disk healthy again: recovery rolls back from the journal.
        calls["failing"] = False
        message = TransactionManager(tmp_path).recover_pending()
        assert message is not None
        assert read(tmp_path, "plan.json") == "old-plan"
        assert read(tmp_path, "pantry.json") == "old-pantry"

    def test_concurrent_writers_serialize_and_never_interleave(self, tmp_path):
        seed(tmp_path, {"plan.json": "seed", "pantry.json": "seed"})
        manager = TransactionManager(tmp_path)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def writer(tag: str) -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(20):
                    manager.save_all({
                        tmp_path / "plan.json": tag,
                        tmp_path / "pantry.json": tag,
                    })
            except BaseException as exc:  # noqa: BLE001 - surface to the assert
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(tag,)) for tag in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors
        # Whole transactions only: both files always end on the same writer.
        assert read(tmp_path, "plan.json") == read(tmp_path, "pantry.json")
        assert not (tmp_path / TX_JOURNAL_FILENAME).exists()

    def test_unsafe_journal_cleanup_reports_recovery_and_freezes_writes(
        self, tmp_path, monkeypatch
    ):
        seed(tmp_path, {"plan.json": "old-plan"})
        manager = TransactionManager(tmp_path)
        journal = tmp_path / TX_JOURNAL_FILENAME
        real_unlink = Path.unlink

        def failing_unlink(path, *args, **kwargs):
            if path == journal:
                raise OSError("journal is locked")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        with pytest.raises(TransactionRecoveryRequiredError) as error:
            manager.save_all({tmp_path / "plan.json": "new-plan"})
        assert error.value.result.status is TransactionStatus.RECOVERY_REQUIRED
        assert error.value.result.files_may_be_committed
        assert read(tmp_path, "plan.json") == "new-plan"
        assert journal.exists()
        assert manager.writes_frozen

        with pytest.raises(TransactionRecoveryRequiredError):
            manager.save_all({tmp_path / "plan.json": "must-not-land"})
        assert read(tmp_path, "plan.json") == "new-plan"


class TestRecoverPending:
    def test_no_journal_is_a_noop(self, tmp_path):
        assert TransactionManager(tmp_path).recover_pending() is None

    def test_restores_old_contents_and_deletes_created_files(self, tmp_path):
        seed(tmp_path, {"plan.json": "torn-new", "leftovers.json": "torn-new"})
        journal = {
            "tx_id": "abc",
            "files": {"plan.json": "old-plan", "leftovers.json": None},
        }
        (tmp_path / TX_JOURNAL_FILENAME).write_text(json.dumps(journal), encoding="utf-8")
        message = TransactionManager(tmp_path).recover_pending()
        assert message is not None
        assert read(tmp_path, "plan.json") == "old-plan"
        assert not (tmp_path / "leftovers.json").exists()
        assert not (tmp_path / TX_JOURNAL_FILENAME).exists()

    def test_corrupt_journal_is_quarantined_not_ignored(self, tmp_path):
        seed(tmp_path, {"plan.json": "current"})
        (tmp_path / TX_JOURNAL_FILENAME).write_text("{not json", encoding="utf-8")
        message = TransactionManager(tmp_path).recover_pending()
        assert message is not None
        assert not (tmp_path / TX_JOURNAL_FILENAME).exists()
        quarantined = list(tmp_path.glob("tx_journal.corrupt-*.json"))
        assert len(quarantined) == 1
        # The files the journal could no longer describe were left untouched.
        assert read(tmp_path, "plan.json") == "current"

    def test_journal_naming_unknown_files_is_quarantined(self, tmp_path):
        seed(tmp_path, {"plan.json": "current"})
        journal = {"tx_id": "abc", "files": {"../evil.json": "boom"}}
        (tmp_path / TX_JOURNAL_FILENAME).write_text(json.dumps(journal), encoding="utf-8")
        message = TransactionManager(tmp_path).recover_pending()
        assert message is not None
        assert not (tmp_path.parent / "evil.json").exists()
        assert read(tmp_path, "plan.json") == "current"
