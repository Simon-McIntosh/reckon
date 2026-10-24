"""A session id quoted inside a refusal is not a queued refusal of the session.

A queued request's launch is re-checked whenever a slot opens, and one of the
refusals it can meet is an ``UnreconciledRuns``: finished runs whose reviews are
still in flight must be reconciled before the pass admits more work. The
refusal names each of those runs together with its next action, and a review
action carries ``--session <id>`` — the dispatching session's own id, because
the review is owed to that session.

The reader that turns a queued failure into a duty used to ask only whether the
session's id appeared anywhere in the refusal's text. It does: quoted inside the
review command the refusal names as another run's action. So the queued run was
published as a session-side refusal with the follower attach line as its remedy.
Nothing a follower does clears an unreconciled-runs refusal; the refusal names
its own two remedies — reconcile each named run, or waive. A queued run cannot
take the waiver after it is queued, so the coordinator's path is the first
remedy and nothing else: a re-dispatch would discard its place while admission
re-tests the stored request by itself.

The ticket is the exception class the admission pass stamps at the head of the
stored failure, ``<ClassName>: <text>``, which says whose refusal it is without
reading the text at all. ``UnreconciledRuns`` publishes the first named run's
own action while that run still has a live pointer; a follower refusal, stamped
as ``_FollowerAdmissionUnmet``, keeps the follower remedy it publishes today.
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
from reckon.crew.node import UnreconciledRuns
from reckon.crew.plan_review import review_invocation
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "quoted-session-fixture"
SESSION = "coordinator-session"
PLAN = "fixture-plan"
OBSERVED_AT = datetime(2026, 10, 23, 15, 50, tzinfo=UTC)


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
        ("commit", "-q", "-m", "test: seed quoted-session fixture"),
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
    exception's type name at its head, composed the same way ``admit_once``
    composes it — so the test reads the exception the classifier must key on
    rather than hand-written text standing in for one.
    """
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "phase": "queued",
            "queued_at": "2026-10-23T14:00:00+00:00",
            "reason": "waiting for a local lane slot",
            "failure": failure,
            "failure_at": "2026-10-23T15:40:00+00:00",
            "failure_attempts": 1,
            "retry_at": "2026-10-23T16:00:00+00:00",
            "node": {
                "id": run_id,
                "plan": PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _write_live_named_run(run_id: str) -> None:
    """Write the live pointer the refusal's named run still holds.

    A named run that has been reconciled no longer has a pointer, and its
    action is then no longer owed; the fixture writes the pointer so the run
    the refusal names is still open when the remedy is read.
    """
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "phase": "working",
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


def _review_action() -> str:
    """The next action of the in-flight run, carrying this session's own id."""
    return review_invocation(PROJECT, PLAN, session=SESSION)


def _unreconciled_refusal() -> str:
    """An ``UnreconciledRuns`` raised by the class, naming a review run.

    The refusal quotes no command of its own; it names the run and the review
    that run is owed in the same line. The session's id reaches the text only
    through that action, which is exactly the coincidence the classifier must
    not mistake for a refusal of the session.
    """
    refusal = UnreconciledRuns(
        [
            {
                "run_id": "r-review-in-flight",
                "next_action": _review_action(),
            }
        ],
        "10m",
    )
    assert SESSION in str(refusal), "fixture must quote the session id"
    return _failure_of(refusal)


def _follower_refusal() -> str:
    """A refusal whose subject is the session, raised by its own class."""
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


def _row(run_id: str) -> dict[str, Any]:
    classified = recovery.classify_pointer(runs.read_pointer(run_id))
    assert classified["run_id"] == run_id
    return classified


def _duties(project: Any) -> list[dict[str, Any]]:
    owed = obligations_module.obligations(PROJECT, SESSION)
    return list(owed["obligations"])


def test_an_unreconciled_runs_refusal_publishes_the_named_runs_action(
    repository: Path,
) -> None:
    """The quoted session id is another run's argument, not this refusal's subject.

    The duty's command is the review the refusal names for the run that is
    still owed one. It is neither the follower the session already has nor the
    waiver only a new dispatch can take.
    """
    _write_live_named_run("r-review-in-flight")
    _write_queued("r-unreconciled", failure=_unreconciled_refusal())
    row = _row("r-unreconciled")
    assert row["classification"] == "queued"
    assert row["session_refusal"] is not None, row
    assert row["next_action"] == _review_action(), row["next_action"]

    owed = _duties(repository)
    listed = [item for item in owed if item["run_id"] == "r-unreconciled"]
    assert len(listed) == 1, owed
    assert listed[0]["kind"] == obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND
    assert listed[0]["next_command"] == _review_action(), listed[0]["next_command"]


def test_the_unreconciled_runs_detail_advises_no_redispatch(
    repository: Path,
) -> None:
    """The detail never tells a queued run to discard itself and dispatch again.

    A queued request cannot take ``--allow-unreconciled-runs`` on a launch that
    has already happened, and re-dispatching it discards its place in the queue
    while admission re-tests the stored request on its own. The refusal quotes
    that waiver, so the detail carries the refusal's words; what it must not do
    is advise acting on it. The assertion is on the classified row because the
    obligations duty row carries no detail field.
    """
    _write_live_named_run("r-review-in-flight")
    _write_queued("r-unreconciled", failure=_unreconciled_refusal())
    row = _row("r-unreconciled")
    detail = str(row["session_refusal"]["detail"])
    assert "discard" not in detail.lower(), detail
    assert "dispatch again" not in detail.lower(), detail


def test_the_unreconciled_runs_remedy_is_never_the_follower_or_the_waiver(
    repository: Path,
) -> None:
    """Neither published line is the one that cannot clear this refusal."""
    _write_queued("r-unreconciled", failure=_unreconciled_refusal())
    row = _row("r-unreconciled")
    action = str(row["next_action"])
    assert action != runs._watch_attach_line(PROJECT, session=SESSION), action
    assert "crew follow" not in action, action
    assert "--allow-unreconciled-runs" not in action, action


def test_a_refusal_whose_subject_is_the_session_keeps_the_follower_remedy(
    repository: Path,
) -> None:
    """A follower condition still publishes the attach line, under the same kind."""
    _write_queued("r-follower", failure=_follower_refusal())
    row = _row("r-follower")
    assert row["classification"] == "queued"
    assert row["next_action"] == runs._watch_attach_line(PROJECT, session=SESSION)

    owed = _duties(repository)
    listed = [item for item in owed if item["run_id"] == "r-follower"]
    assert len(listed) == 1, owed
    assert listed[0]["kind"] == obligations_module.QUEUED_AWAITING_SESSION_DUTY_KIND
    assert listed[0]["next_command"] == runs._watch_attach_line(
        PROJECT, session=SESSION
    ), listed[0]["next_command"]


def test_a_lane_hold_still_reads_as_waiting_for_a_slot(repository: Path) -> None:
    """A lane hold is the lane speaking, whatever its message quotes."""
    _write_queued("r-lane-hold", failure=_lane_hold())
    row = _row("r-lane-hold")
    assert row["classification"] == "queued"
    assert row.get("session_refusal") is None, row
    assert row["next_action"] == "wait for a local lane slot", row["next_action"]
