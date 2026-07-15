"""Purchase log store: a corrupted log is NEVER a legal empty one."""

import json

from models.purchase_log import PurchaseInput, PurchaseRecord, new_purchase_event_id
from services.purchase_log_store import (
    PURCHASE_PHOTOS_DIRNAME,
    PurchaseLogStore,
    sweep_orphan_photos,
)


def make_record(food_id="rice_white", photo_path=None, voided_at=None) -> PurchaseRecord:
    event_id = new_purchase_event_id()
    return PurchaseRecord(
        event_id=event_id,
        food_id=food_id,
        raw_name="Rice",
        brand="Great Value",
        package_label="2 lb bag",
        grams=907.0,
        quantity=1,
        line_total=3.49,
        estimated_line_cost=None,
        price_source="user_entered",
        store="Kroger",
        photo_path=photo_path,
        group_id=event_id,
        origin="product_photo",
        purchased_at="2026-07-11T10:00:00",
        plan_id="plan-1",
        pantry_grams_before=100.0,
        voided_at=voided_at,
    )


class TestRoundTrip:
    def test_save_and_load(self, tmp_path):
        store = PurchaseLogStore(tmp_path)
        records = [make_record(), make_record(photo_path="purchase_photos/a.jpg",
                                              voided_at="2026-07-12T09:00:00")]
        store.save(records)
        result = PurchaseLogStore(tmp_path).load()
        assert result.load_error is None
        assert result.records == records

    def test_missing_file_is_a_legal_empty_log(self, tmp_path):
        result = PurchaseLogStore(tmp_path).load()
        assert result.records == [] and result.load_error is None

    def test_photo_paths_stay_relative(self, tmp_path):
        store = PurchaseLogStore(tmp_path)
        store.save([make_record(photo_path="purchase_photos/x.jpg")])
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["records"][0]["photo_path"] == "purchase_photos/x.jpg"
        assert "\\" not in raw["records"][0]["photo_path"]


class TestCorruption:
    def test_unparseable_file_reports_error_and_preserves_it(self, tmp_path):
        store = PurchaseLogStore(tmp_path)
        store.path.write_text("{definitely not json", encoding="utf-8")
        result = store.load()
        assert result.load_error is not None
        assert result.records == []
        # The corrupt file must survive untouched for recovery.
        assert store.path.read_text(encoding="utf-8") == "{definitely not json"

    def test_unknown_version_reports_error(self, tmp_path):
        store = PurchaseLogStore(tmp_path)
        store.path.write_text(json.dumps({"version": 99, "records": []}), encoding="utf-8")
        assert store.load().load_error is not None

    def test_one_malformed_record_fails_the_whole_load(self, tmp_path):
        """Partial history would corrupt undo baselines — all or nothing."""
        store = PurchaseLogStore(tmp_path)
        good = make_record().to_dict()
        bad = {"event_id": "e2"}  # missing everything
        store.path.write_text(
            json.dumps({"version": 1, "records": [good, bad]}), encoding="utf-8"
        )
        result = store.load()
        assert result.load_error is not None
        assert result.records == []


class TestSweep:
    def _photo(self, tmp_path, name: str) -> None:
        photos = tmp_path / PURCHASE_PHOTOS_DIRNAME
        photos.mkdir(parents=True, exist_ok=True)
        (photos / name).write_bytes(b"img")

    def test_sweep_removes_tmp_and_unreferenced_only(self, tmp_path):
        store = PurchaseLogStore(tmp_path)
        kept = make_record(photo_path=f"{PURCHASE_PHOTOS_DIRNAME}/keep.jpg")
        self._photo(tmp_path, "keep.jpg")
        self._photo(tmp_path, "orphan.jpg")
        self._photo(tmp_path, ".tmp-half-written.jpg")
        sweep_orphan_photos(store, [kept])
        photos = tmp_path / PURCHASE_PHOTOS_DIRNAME
        assert (photos / "keep.jpg").exists()
        assert not (photos / "orphan.jpg").exists()
        assert not (photos / ".tmp-half-written.jpg").exists()

    def test_sweep_without_directory_is_a_noop(self, tmp_path):
        sweep_orphan_photos(PurchaseLogStore(tmp_path), [])  # must not raise


class TestPurchaseInputDefaults:
    def test_group_defaults_are_explicit(self):
        purchase_input = PurchaseInput(event_id="e1", food_id="rice_white", grams=100.0)
        assert purchase_input.apply_to_plan is False
        assert purchase_input.line_total is None
        assert purchase_input.price_source == "unknown"
