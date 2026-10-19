"""A queued request the session itself must unblock says so on every surface.

A queued request normally waits on the lane, and both the live view and the
obligations view read it that way. When the admission pass refused it for a
condition of the session rather than of the lane, that reading sends the
coordinator off to wait on something no wait clears: the refusal and its remedy
lived only in the pointer's ``failure`` field. The tests below hold two queued
pointers for one session — one refused for a session condition, one held by the
lane alone — and require the surfaces to tell them apart.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, runs
from reckon.crew.dispatch_admission import LaneHeld
from reckon.crew.dispatch_watch import _FollowerAdmissionUnmet
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "queued-refusal-fixture"
SESSION = "coordinator-session"
OBSERVED_AT = datetime(2026, 10, 19, 5, 50, tzinfo=UTC)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed queued-refusal fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_queued(run_id: str, *, failure: str) -> None:
    """Write one queued pointer carrying the admission failure it was left with.

    The failure is spelled exactly as the admission pass stamps it — the
    exception's type name, the same way ``admit_once`` composes it — so the
    test reads the refusal a real pass would have recorded rather than a
    hand-written stand-in for one.
    """
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "phase": "queued",
            "queued_at": "2026-10-19T04:00:00+00:00",
            "reason": "waiting for a local lane slot",
            "failure": failure,
            "failure_at": "2026-10-19T05:40:00+00:00",
            "failure_attempts": 3,
            "retry_at": "2026-10-19T06:00:00+00:00",
            "node": {
                "id": run_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _failure_of(exception: BaseException) -> str:
    """The recorded failure text for one raised refusal, as a pass composes it."""
    return f"{type(exception).__name__}: {exception}"


def _follower_refusal() -> str:
    return _failure_of(
        _FollowerAdmissionUnmet(
            PROJECT,
            {"attach_line": "reckon crew follow --project fixture"},
            session=SESSION,
            conditions=[f"session {SESSION!r} has no registered follower"],
        )
    )


def _lane_hold() -> str:
    return _failure_of(
        LaneHeld(
            {
                "state": "full",
                "detail": (
                    f"the lane grants session {SESSION!r} no worker slot; "
                    "the gate is open and the lane will launch it when a slot frees"
                ),
            }
        )
    )


def _row(project: str, run_id: str) -> dict[str, Any]:
    classified = recovery.classify_pointer(runs.read_pointer(run_id))
    assert classified["run_id"] == run_id
    return classified


def test_a_session_side_refusal_names_the_follow_command(repository: Path) -> None:
    """The published next action is the arming command the rest of the crew uses.

    Asserted against the shared helper rather than against a substring: the
    surface and the dispatch record have to name one command, and two
    spellings that agree today are free to drift apart.
    """
    _write_queued("r-queued-session-refusal", failure=_follower_refusal())
    row = _row(PROJECT, "r-queued-session-refusal")
    assert row["classification"] == "queued"
    assert row["next_action"] == runs._watch_attach_line(PROJECT, session=SESSION)


def test_the_refused_request_appears_in_the_sessions_obligations(
    repository: Path,
) -> None:
    _write_queued("r-queued-session-refusal", failure=_follower_refusal())
    owed = obligations_module.obligations(PROJECT, SESSION)
    listed = [
        item
        for item in owed["obligations"]
        if item["run_id"] == "r-queued-session-refusal"
    ]
    assert len(listed) == 1, owed["obligations"]
    assert "crew follow" in listed[0]["next_command"], listed[0]["next_command"]
    assert listed[0]["kind"] == obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND


def test_a_lane_hold_keeps_the_waiting_reading(repository: Path) -> None:
    _write_queued("r-lane-held", failure=_lane_hold())
    row = _row(PROJECT, "r-lane-held")
    assert row["classification"] == "queued"
    assert row["next_action"] == "wait for a local lane slot", row["next_action"]
    owed = obligations_module.obligations(PROJECT, SESSION)
    assert not [
        item for item in owed["obligations"] if item["run_id"] == "r-lane-held"
    ], owed["obligations"]


def test_the_two_readings_hold_side_by_side(repository: Path) -> None:
    _write_queued("r-queued-session-refusal", failure=_follower_refusal())
    _write_queued("r-lane-held", failure=_lane_hold())
    owed = obligations_module.obligations(PROJECT, SESSION)
    kinds = {item["run_id"]: item["kind"] for item in owed["obligations"]}
    assert kinds == {
        "r-queued-session-refusal": obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND
    }, kinds
