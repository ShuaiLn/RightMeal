"""Start-tab draft state and household summary — page-free by design.

This module must never import flet: both ``ui.state`` (which stores a
PlanDraft) and ``ui.start_view`` (which builds one) import it, so the import
graph stays acyclic by construction: state.py -> plan_draft.py <- start_view.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from models.profile import HouseholdProfile
from planner.recipe_scheduler import VarietyMode

# Single source of truth for the plan-mode segmented button — also used to
# validate a restored draft, so the two can never drift apart.
PLAN_MODES = ("weekly", "daily")


@dataclass
class PlanDraft:
    """A snapshot of in-progress Start-tab inputs, taken when the user leaves
    to edit their household profile, so navigating back restores them instead
    of resetting to defaults."""

    budget_text: str
    zip_text: str
    zip_dirty: bool
    mode: str
    variety: str
    start_date: date
    end_date: date


def snapshot_draft(
    budget_text: str,
    zip_text: str,
    profile_zip: str | None,
    mode: str,
    variety: str,
    start_date: date,
    end_date: date,
) -> PlanDraft:
    # A ZIP is a validated 5-digit string, so stripping loses nothing — and
    # "91765 " must not count as an edit of "91765".
    normalized_zip = (zip_text or "").strip()
    return PlanDraft(
        budget_text=budget_text,
        zip_text=normalized_zip,
        zip_dirty=normalized_zip != ((profile_zip or "").strip()),
        mode=mode,
        variety=variety,
        start_date=start_date,
        end_date=end_date,
    )


def restore_draft_values(
    draft: PlanDraft, profile: HouseholdProfile, today: date
) -> tuple[str, str, str, str, date, date]:
    """The (budget_text, zip_text, mode, variety, start_date, end_date) to
    populate the Start-tab controls with, from a preserved draft. Falls back
    to a safe default per-field rather than trusting stale/invalid values.
    ``today`` is injected so restores are deterministic under test."""
    mode = draft.mode if draft.mode in PLAN_MODES else "weekly"
    variety = draft.variety if draft.variety in {m.value for m in VarietyMode} else "balanced"
    if draft.start_date <= draft.end_date:
        start_date, end_date = draft.start_date, draft.end_date
    else:
        start_date, end_date = today, today + timedelta(days=6)
    zip_text = draft.zip_text if draft.zip_dirty else profile.zip_code
    return draft.budget_text, zip_text, mode, variety, start_date, end_date


def household_summary_text(profile: HouseholdProfile) -> str:
    """"4 servings (2 adults, 2 children)" — says "servings" because that is
    what the planner scales portions by."""
    total = profile.total_members
    if total < 1:
        return "No household members yet"
    parts: list[str] = []
    if profile.adults:
        parts.append(f"{profile.adults} adult" + ("s" if profile.adults != 1 else ""))
    if profile.children:
        parts.append(f"{profile.children} " + ("children" if profile.children != 1 else "child"))
    if profile.seniors:
        parts.append(f"{profile.seniors} senior" + ("s" if profile.seniors != 1 else ""))
    servings = f"{total} serving" + ("s" if total != 1 else "")
    return f"{servings} ({', '.join(parts)})"
