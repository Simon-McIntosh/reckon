"""A failed launch of a run that has worked is not written back as a request."""

from __future__ import annotations

import pytest

from reckon.crew.admission import launching


@pytest.mark.parametrize(
    "pointer",
    [
        {"session_id": "01a1e185-37a9", "attempt": 1, "attempt_kind": "dispatch"},
        {"attempt": 2, "attempt_kind": "dispatch"},
        {"attempt": 1, "attempt_kind": "resume"},
    ],
    ids=["holds-a-session", "later-attempt", "started-as-a-resume"],
)
def test_a_run_that_has_worked_is_recognised(pointer):
    assert launching._failed_launch_follows_work(pointer)


@pytest.mark.parametrize(
    "pointer",
    [
        {},
        {"attempt": 1, "attempt_kind": "dispatch"},
        {"attempt": 1, "attempt_kind": "dispatch", "session_id": None},
        {"attempt": 1, "session_id": "  "},
        {"attempt": True},
    ],
    ids=["bare", "first-dispatch", "no-session", "blank-session", "boolean-attempt"],
)
def test_a_first_launch_that_never_started_is_a_request(pointer):
    assert not launching._failed_launch_follows_work(pointer)


def test_a_failed_resume_is_left_as_it_is(monkeypatch):
    written = []
    monkeypatch.setattr(
        launching, "_writeback_pointer", lambda run_id, record: written.append(run_id)
    )
    worked = {
        "run_id": "r-worked",
        "phase": launching.LAUNCH_FAILED_PHASE,
        "session_id": "01a1e185-37a9",
        "attempt": 2,
        "worktree": "/somewhere/worktree",
        "admit_request": {"run_id": "r-worked", "node": {}},
    }

    assert launching._requeue_admitted_failures([worked]) == []
    assert written == []
