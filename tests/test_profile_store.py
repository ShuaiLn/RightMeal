"""Profile persistence tests using a temporary directory."""

import json

from models import HouseholdProfile
from services.keys import resolve_key
from services.profile_store import ProfileStore


def make_profile():
    return HouseholdProfile(
        adults=2,
        children=2,
        vegetarian=True,
        allergies=["peanut"],
        city="Los Angeles",
        zip_code="90001",
        api_keys={"openai_api_key": "sk-test"},
    )


def test_first_run_returns_none(tmp_path):
    assert ProfileStore(tmp_path).load() is None


def test_round_trip(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(make_profile())
    loaded = store.load()
    assert loaded is not None
    assert loaded.adults == 2
    assert loaded.children == 2
    assert loaded.vegetarian is True
    assert loaded.allergies == ["peanut"]
    assert loaded.zip_code == "90001"
    assert loaded.api_keys["openai_api_key"] == "sk-test"


def test_corrupt_file_returns_none(tmp_path):
    store = ProfileStore(tmp_path)
    store.base_dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() is None


def test_delete_removes_saved_data(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(make_profile())
    assert store.load() is not None
    store.delete()
    assert store.load() is None
    store.delete()  # idempotent


def test_saved_file_is_plain_local_json(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(make_profile())
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["allergies"] == ["peanut"]


def test_resolve_key_prefers_profile_then_env(tmp_path, monkeypatch):
    profile = make_profile()
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert resolve_key("openai_api_key", profile) == "sk-test"
    profile.api_keys["openai_api_key"] = ""
    assert resolve_key("openai_api_key", profile) == "env-key"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert resolve_key("openai_api_key", profile) is None
