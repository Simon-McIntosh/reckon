"""A plan's review composes itself once its author stops editing.

A coordinator must remember to compose a content review after editing a plan,
and a coordinator paused at a rate limit cannot. The producer pass — the crew
watch sweep — composes the review a plan owes once it has been left alone for
the quiet period, composes nothing for a plan still being authored or already
under review, and answers the findings its reviewer marked as not changing the
plan so the coordinator is woken only for the ones that would.
"""

from __future__ import annotations

import importlib
import time
from datetime import UTC, datetime

import pytest

from reckon.crew import plan_review, recovery, recovery_review_dispatch, resumption
from tests.test_review_plan_command import CONFIG
from tests.test_review_plan_command import project as project  # noqa: PLC0414

QUIET_EDIT_SECONDS = 11 * 60
FRESH_EDIT_SECONDS = 2 * 60


def _edit(path, seconds_ago: float) -> None:
    """Give the plan the modification time an edit that many seconds ago leaves."""
    stamp = time.time() - seconds_ago
    import os

    os.utime(path, (stamp, stamp))


def _launch(monkeypatch) -> list[str]:
    """Stand in for the launcher, so a pass dispatches without spawning."""
    dispatch = importlib.import_module("reckon.crew.dispatch")
    launched: list[str] = []

    def dispatch_instead(**kwargs):
        launched.append(str(kwargs.get("session") or ""))
        return {"run_id": "composed-review"}

    monkeypatch.setattr(dispatch, "dispatch", dispatch_instead)
    return launched


def _pass(project_name: str = "sample") -> dict:
    """Run one producer pass and return what it composed for plans."""
    return resumption.sweep(project_name, session="coordinator", config=CONFIG)[
        "plan_reviews"
    ]


def _run_directories():
    root = plan_review.review_report_directory("sample", "fixture", "unused").parent
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _stored_review(**kwargs) -> None:
    record = {
        "project": "sample",
        "plan_slug": "fixture",
        "plan_version": 3,
        "rubric": "content",
        "findings": [],
        **kwargs,
    }
    plan_review.store_plan_review(record)


def test_a_quiet_plan_owing_review_is_composed_on_the_next_pass(project, monkeypatch):
    _home, _repo, path = project
    launched = _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)

    report = _pass()

    composed = [row for row in report["composed"] if row["plan"] == "fixture"]
    assert composed, report
    assert composed[0]["dispatched"] is True
    assert composed[0]["review_run_id"] == "composed-review"
    # The dispatch named no lane, so the review takes the review lanes rather
    # than a metered one the caller chose.
    assert launched == ["coordinator"]
    # A review was really composed, not merely reported: its brief, its plan
    # snapshot and its sidecar are on disk for the queued run to read.
    directories = _run_directories()
    assert len(directories) == 1
    assert (directories[0] / "brief.md").is_file()
    assert (directories[0] / "plan.html").read_bytes() == path.read_bytes()
    sidecar = (directories[0] / "plan-review.json").read_text()
    assert '"rubric": "content"' in sidecar


def test_a_plan_edited_two_minutes_ago_is_not_composed(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, FRESH_EDIT_SECONDS)

    report = _pass()

    assert [row for row in report["composed"] if row["plan"] == "fixture"] == []
    skipped = [row for row in report["skipped"] if row.get("plan") == "fixture"]
    assert skipped, report
    assert "minutes ago" in skipped[0]["reason"]
    assert _run_directories() == []


def test_a_plan_with_a_review_in_flight_is_not_composed(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    monkeypatch.setattr(
        recovery_review_dispatch,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": "coordinator",
                "node": {"id": "plan-review-of-fixture"},
                "phase": "working",
            }
        ],
    )

    report = _pass()

    assert [row for row in report["composed"] if row["plan"] == "fixture"] == []
    skipped = [row for row in report["skipped"] if row.get("plan") == "fixture"]
    assert skipped and "standing-review" in skipped[0]["reason"]
    assert _run_directories() == []


def test_would_not_change_findings_are_noted_and_the_rest_stay_open(
    project, monkeypatch
):
    _home, _repo, path = project
    _launch(monkeypatch)
    # A fresh edit keeps composition out of the way: the notes are answered by
    # a pass that composes nothing, so an answer cannot be mistaken for one.
    _edit(path, FRESH_EDIT_SECONDS)
    _stored_review(
        findings=[
            {
                "id": "scope-1",
                "type": "scope",
                "text": "The plan names a file it does not change.",
                "would_change": False,
                "reason": "the plan names the file as context only.",
            },
            {
                "id": "scope-2",
                "type": "scope",
                "text": "Section five duplicates section two.",
                "would_change": True,
                "reason": "the duplication changes what the node builds.",
            },
            {
                "id": "scope-3",
                "type": "scope",
                "text": "The review stated no verdict on this one.",
                "would_change": None,
            },
        ]
    )

    report = _pass()

    noted = [row["finding"] for row in report["notes"]]
    assert noted == ["scope-1"]
    record = plan_review.list_plan_reviews("sample")[0]
    assert record["responses"]["scope-1"]["action"] == plan_review.NOTED_ACTION
    assert (
        record["responses"]["scope-1"]["reason"]
        == "the plan names the file as context only."
    )
    assert record["responses"]["scope-1"]["by"] == plan_review.PLAN_REVIEW_REFLEX
    # The finding that would change the plan, and the one whose report stated
    # no verdict, both wait for their author.
    assert plan_review.unanswered_findings(record) == ["scope-2", "scope-3"]


def test_a_plan_whose_review_is_stored_is_not_composed_again(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    _stored_review(
        plan_fingerprint=plan_review.plan_fingerprint(path),
        reviewed_blob_sha="a" * 40,
        timestamp=datetime.now(UTC).isoformat(),
    )

    report = _pass()

    assert [row for row in report["composed"] if row["plan"] == "fixture"] == []
    skipped = [row for row in report["skipped"] if row.get("plan") == "fixture"]
    assert skipped and "already covers" in skipped[0]["reason"]


def test_the_quiet_period_is_measured_from_the_plan_file(project):
    _home, _repo, path = project
    _edit(path, QUIET_EDIT_SECONDS)
    assert plan_review.plan_review_quiet_remaining(path) == 0
    _edit(path, FRESH_EDIT_SECONDS)
    remaining = plan_review.plan_review_quiet_remaining(path)
    assert 0 < remaining <= plan_review.PLAN_REVIEW_QUIET_SECONDS - FRESH_EDIT_SECONDS


@pytest.mark.parametrize("seconds_ago", [QUIET_EDIT_SECONDS, FRESH_EDIT_SECONDS])
def test_a_pass_without_a_session_composes_nothing(project, monkeypatch, seconds_ago):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, seconds_ago)
    monkeypatch.setattr(recovery, "_sweeping_session", lambda project: "")

    report = plan_review.compose_owed_plan_reviews("sample", session="")

    assert report["composed"] == []
    assert report["skipped"] and "no session" in report["skipped"][0]["reason"]
