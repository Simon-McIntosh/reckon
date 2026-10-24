"""A full queue and a claimed write path are waits, not session refusals.

A queued request's launch is retried on every pass, and the retry can be
refused because the session already holds its share of queued dispatches
(``QueueFull``) or because a live run claims a write path the request needs
(``ScopeConflict``), or that anchor run's directory claim, which is the same
refusal in a looser claim kind (``DirectoryClaimConflict``). Each names the
session or quotes the commands that lead to its own retry, so a reader that
judged the failure by its text published "arm the follower" or a bare dispatch
as the remedy for a wait nothing the session arms can clear. Each clears when a
lane slot opens or the claiming run ends, so each keeps the waiting reading and
the request stays a queued run.

The follower condition is the case the session does owe, and it must keep
reading that way. The retry back-off the admission pass stamps on a failed
request is the reason these refusals are named in the classifier rather than
added to the lane-hold test: widening that test would drop the back-off.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, recovery, runs
from reckon.crew.dispatch_claims import DirectoryClaimConflict
from reckon.crew.dispatch_watch import _FollowerAdmissionUnmet
from reckon.crew.node import ScopeConflict
from reckon.crew.runs import QueueFull
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "queue-wait-fixture"
SESSION = "coordinator-session"
PLAN = "fixture-plan"
# The instant the whole fixture is read at: after every recorded retry instant
# below, so the classifier's own reading is never confused with a back-off.
OBSERVED_AT = datetime(2026, 10, 24, 1, 30, tzinfo=UTC)
PEER_RUN = "r-20261023T220036362628-a-peer-holds-the-path"
CLAIMED_PATH = "reckon/crew/recovery_watch.py"


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
        ("commit", "-q", "-m", "test: seed queue-wait fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_queued(run_id: str, *, failure: str) -> None:
    """Write one queued pointer carrying the admission failure it was left with.

    The failure is spelled as the admission pass stamps it -- the exception's
    type name at the head -- so each test reads a refusal a real pass could
    have recorded rather than a hand-written stand-in for one.
    """
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "phase": "queued",
            "queued_at": "2026-10-24T00:05:00+00:00",
            "reason": "waiting for a local lane slot",
            "failure": failure,
            "failure_at": "2026-10-24T01:00:00+00:00",
            "failure_attempts": 2,
            "retry_at": "2026-10-24T01:10:00+00:00",
            "node": {
                "id": run_id,
                "plan": PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _failure_of(exception: BaseException) -> str:
    """The recorded failure text for one raised refusal, as a pass composes it."""
    return f"{type(exception).__name__}: {exception}"


def _queue_full() -> str:
    """The refusal a session holding its share of queued runs is given."""
    return _failure_of(QueueFull(session=SESSION, held=2, cap=2))


def _scope_conflict() -> str:
    """The refusal a live run's claim on a needed write path gives."""
    return _failure_of(
        ScopeConflict(
            run_id=PEER_RUN,
            node_id="a-peer-holds-the-path",
            candidate_path=CLAIMED_PATH,
            claimed_path=CLAIMED_PATH,
        )
    )


def _directory_claim_conflict() -> str:
    """A looser claim kind's refusal, which is the same wait on a live run."""
    return _failure_of(
        DirectoryClaimConflict(
            run_id=PEER_RUN,
            node_id="a-peer-holds-the-path",
            candidate_path="docs/figures",
            claimed_path="docs/figures/a-peer/a-figure.svg",
            alternatives=["docs/figures/a-peer/a-figure.svg"],
        )
    )


def _unregistered_follower() -> str:
    """The refusal a follower condition gives, which the session must clear."""
    return _failure_of(
        _FollowerAdmissionUnmet(
            PROJECT,
            {"attach_line": "reckon crew follow --project fixture"},
            session=SESSION,
            conditions=[f"session {SESSION!r} has no registered follower"],
        )
    )


def _row(run_id: str) -> dict[str, Any]:
    classified = recovery.classify_pointer(runs.read_pointer(run_id))
    assert classified["run_id"] == run_id
    return classified


def _duty_kind(run_id: str) -> str:
    owed = obligations_module.obligations(PROJECT, SESSION)
    listed = [item for item in owed["obligations"] if item["run_id"] == run_id]
    assert len(listed) == 1, owed["obligations"]
    return str(listed[0]["kind"])


@pytest.mark.parametrize(
    ("run_id", "failure"),
    [
        ("r-queue-full", _queue_full),
        ("r-scope-conflict", _scope_conflict),
        ("r-directory-claim", _directory_claim_conflict),
    ],
)
def test_a_wait_refusal_keeps_the_queued_reading(
    repository: Path, run_id: str, failure: Any
) -> None:
    """Neither refusal raises a session duty: each clears on its own."""
    _write_queued(run_id, failure=failure())
    row = _row(run_id)
    assert row["classification"] == "queued"
    assert row.get("session_refusal") is None, row
    assert row["next_action"] == "wait for a local lane slot", row["next_action"]


@pytest.mark.parametrize(
    ("run_id", "failure"),
    [
        ("r-queue-full", _queue_full),
        ("r-scope-conflict", _scope_conflict),
        ("r-directory-claim", _directory_claim_conflict),
    ],
)
def test_a_wait_refusal_lists_as_a_queued_run(
    repository: Path, run_id: str, failure: Any
) -> None:
    """The obligations view lists it under the wait, not under the session."""
    _write_queued(run_id, failure=failure())
    assert _duty_kind(run_id) == obligations_module.QUEUED_RUN_DUTY_KIND


def test_an_unregistered_follower_still_lists_as_the_sessions_duty(
    repository: Path,
) -> None:
    """A follower condition names work only the session can do, and keeps it."""
    _write_queued("r-follower", failure=_unregistered_follower())
    row = _row("r-follower")
    assert row["classification"] == "queued"
    assert row.get("session_refusal") is not None, row
    assert row["next_action"] == runs._watch_attach_line(PROJECT, session=SESSION)
    assert (
        _duty_kind("r-follower") == obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND
    )


def test_a_queue_full_failure_is_not_a_lane_hold(repository: Path) -> None:
    """The retry back-off still applies to a full queue.

    ``_is_lane_hold`` is what the admission pass asks before skipping a failed
    request's back-off. A ``QueueFull`` refusal must keep answering no, or the
    request would be re-launched on every pass while the session is at its cap.
    """
    failure = _queue_full()
    assert queue_admission._is_lane_hold(failure) is False
    pointer = {
        "run_id": "r-queue-full",
        "session": SESSION,
        "phase": "queued",
        "failure": failure,
        "retry_at": "2026-10-24T02:00:00+00:00",
    }
    held = queue_admission._retry_backoff_hold(
        pointer, moment=OBSERVED_AT, config_for=None
    )
    assert held is not None, "a full queue must be left until its retry instant"
    assert "QueueFull" in held
