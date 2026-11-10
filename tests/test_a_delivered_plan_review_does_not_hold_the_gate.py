"""A delivered plan review does not hold the gate for the next round.

The next plan review could not be composed until the previous review run was
promoted, because the in-flight check counted any live review pointer for the
plan, including one whose report was already stored. Promotion took ten to
fifteen minutes, and a promotion that failed partway left the gate shut until it
was re-run by hand, so every round cost reviewer time plus promotion time on the
coordinator's critical path.

A review whose record is stored and readable has delivered. What remains is its
promotion, which is owed and listed as it is today; it no longer stands in the
way of the next compose. A review still working, or one whose record is missing
or unreadable, keeps the gate exactly as before.

The control for this file restores the any-live-pointer check, which makes the
first case refuse again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import plan_review, recovery_review_dispatch, runs

PROJECT = "plan-review-gate-fixture"
SESSION = "coordinator-session"
PLAN = "fixture-plan"
REVIEW_RUN_ID = "r-plan-review-delivered"
VERSION = 3


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_live_review() -> str:
    """One plan review of the plan, live and not yet promoted."""
    runs._write_json(
        runs.pointer_path(REVIEW_RUN_ID),
        {
            "run_id": REVIEW_RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "phase": "running",
            "node": {
                "id": f"plan-review-of-{PLAN}",
                "plan": "",
                "section": "",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )
    return REVIEW_RUN_ID


def _store_review_record(review_run_id: str = REVIEW_RUN_ID) -> Path:
    """The record the reviewer wrote, in the store the gate reads."""
    return plan_review.store_plan_review(
        {
            "project": PROJECT,
            "plan_slug": PLAN,
            "plan_version": VERSION,
            "review_run_id": review_run_id,
            "rubric": "plan_review",
            "status": "ready",
        }
    )


def _unreadable_record() -> Path:
    """A record file at the store path that no reader can parse."""
    path = plan_review.plan_review_path(PROJECT, PLAN, VERSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    return path


def test_a_delivered_unpromoted_review_lets_the_next_review_compose(
    crew_home: Path,
) -> None:
    """The record is stored, so the review has delivered and stands for nothing."""
    _write_live_review()
    _store_review_record()

    standing = recovery_review_dispatch._standing_plan_review(PROJECT, PLAN)

    assert standing == "", standing


def test_a_review_still_working_refuses_as_before(crew_home: Path) -> None:
    """With no stored record the review is still working, and names itself."""
    _write_live_review()

    standing = recovery_review_dispatch._standing_plan_review(PROJECT, PLAN)

    assert standing == REVIEW_RUN_ID, standing


def test_a_delivered_review_with_an_unreadable_record_still_refuses(
    crew_home: Path,
) -> None:
    """A record that cannot be read is no delivery, so the gate stays shut."""
    _write_live_review()
    _unreadable_record()

    standing = recovery_review_dispatch._standing_plan_review(PROJECT, PLAN)

    assert standing == REVIEW_RUN_ID, standing


def test_a_record_stored_for_another_review_run_does_not_clear_this_one(
    crew_home: Path,
) -> None:
    """A later round's record is not this run's delivery."""
    _write_live_review()
    _store_review_record("r-plan-review-from-an-earlier-round")

    standing = recovery_review_dispatch._standing_plan_review(PROJECT, PLAN)

    assert standing == REVIEW_RUN_ID, standing
