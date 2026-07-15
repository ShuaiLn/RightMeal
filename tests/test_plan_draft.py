"""ui.plan_draft tests: page-free draft snapshot/restore and the household
summary. The module must never import flet (ui.state imports it at startup,
and the import graph state.py -> plan_draft.py <- start_view.py stays acyclic
only while it is UI-toolkit free)."""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from models import HouseholdProfile
from ui.plan_draft import (
    PLAN_MODES,
    PlanDraft,
    household_summary_text,
    restore_draft_values,
    snapshot_draft,
)

TODAY = date(2026, 7, 14)


def make_draft(**overrides) -> PlanDraft:
    base = dict(
        budget_text="50", zip_text="90001", zip_dirty=False, mode="weekly",
        variety="balanced", start_date=date(2026, 7, 7), end_date=date(2026, 7, 13),
    )
    base.update(overrides)
    return PlanDraft(**base)


class TestModuleIsPageFree:
    def test_importing_plan_draft_never_pulls_in_flet(self):
        repo_root = Path(__file__).resolve().parents[1]
        code = (
            "import sys; import ui.plan_draft; "
            "assert 'flet' not in sys.modules, 'ui.plan_draft imported flet'"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root / "src")
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestSnapshotDraft:
    def test_round_trip_through_restore(self):
        profile = HouseholdProfile(adults=2, zip_code="90001")
        draft = snapshot_draft("77", "94103", profile.zip_code, "daily", "meal_prep",
                               date(2026, 7, 7), date(2026, 7, 13))
        values = restore_draft_values(draft, profile, TODAY)
        assert values == ("77", "94103", "daily", "meal_prep",
                          date(2026, 7, 7), date(2026, 7, 13))

    def test_zip_dirty_only_when_actually_edited(self):
        clean = snapshot_draft("50", "90001", "90001", "weekly", "balanced", TODAY, TODAY)
        assert clean.zip_dirty is False
        dirty = snapshot_draft("50", "94103", "90001", "weekly", "balanced", TODAY, TODAY)
        assert dirty.zip_dirty is True

    def test_trailing_whitespace_is_not_an_edit(self):
        draft = snapshot_draft("50", "91765 ", "91765", "weekly", "balanced", TODAY, TODAY)
        assert draft.zip_dirty is False
        assert draft.zip_text == "91765"  # the draft stores the normalized ZIP

    def test_none_profile_zip_handled(self):
        draft = snapshot_draft("50", "", None, "weekly", "balanced", TODAY, TODAY)
        assert draft.zip_dirty is False
        typed = snapshot_draft("50", "90001", None, "weekly", "balanced", TODAY, TODAY)
        assert typed.zip_dirty is True


class TestRestoreDraftValues:
    def test_clean_zip_tracks_the_current_profile(self):
        profile = HouseholdProfile(zip_code="20002")  # changed on the Profile page
        draft = make_draft(zip_text="10001", zip_dirty=False)
        _, zip_text, *_ = restore_draft_values(draft, profile, TODAY)
        assert zip_text == "20002"

    def test_dirty_zip_wins_over_profile(self):
        profile = HouseholdProfile(zip_code="20002")
        draft = make_draft(zip_text="10001", zip_dirty=True)
        _, zip_text, *_ = restore_draft_values(draft, profile, TODAY)
        assert zip_text == "10001"

    def test_invalid_mode_and_variety_fall_back(self):
        draft = make_draft(mode="fortnightly", variety="chaotic")
        _, _, mode, variety, _, _ = restore_draft_values(draft, HouseholdProfile(), TODAY)
        assert mode == "weekly" and mode in PLAN_MODES
        assert variety == "balanced"

    def test_inverted_dates_fall_back_to_injected_today(self):
        draft = make_draft(start_date=date(2026, 7, 20), end_date=date(2026, 7, 7))
        *_, start_date, end_date = restore_draft_values(draft, HouseholdProfile(), TODAY)
        assert start_date == TODAY
        assert end_date == date(2026, 7, 20)  # TODAY + 6 days
        assert (end_date - start_date).days == 6


class TestHouseholdSummaryText:
    def test_single_adult(self):
        assert household_summary_text(HouseholdProfile(adults=1)) == "1 serving (1 adult)"

    def test_family_breakdown(self):
        profile = HouseholdProfile(adults=2, children=2)
        assert household_summary_text(profile) == "4 servings (2 adults, 2 children)"

    def test_singular_child_and_plural_seniors(self):
        profile = HouseholdProfile(adults=0, children=1, seniors=2)
        assert household_summary_text(profile) == "3 servings (1 child, 2 seniors)"

    def test_zero_members(self):
        profile = HouseholdProfile(adults=0)
        assert household_summary_text(profile) == "No household members yet"
