"""A session-scoped follower's row counts that session's work in every project.

Measured 2026-10-18: session ``83d14db1`` worked in power_convertor and reckon
at once, and its pane's worker count swung between 7w and 8w and back within a
minute, because each row drew its own project's figures under one unlabelled
column. One project's watcher cannot see the session's work in another, so the
row's counters must sum the session's entry from every project's events file
under the watch directory -- but only while that project's producer is live, or
a stopped watcher would keep contributing its last figure to every row forever.

The fixture drives two projects whose events files each carry the per-session
partition a real producer writes, plus a third whose newest transition carries
no entry for the session. Each other project's producer is published through the
obligation snapshot the watcher writes, so the liveness reading the row takes is
the one a live watcher supplies. The third project's producer is published too,
so that what keeps its older session entry out of the total is the
newest-transition selection rather than the liveness gate.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import obligation_snapshot, recovery, runs

SESSION = "83d14db1"
PEER_SESSION = "peer-session"


def _entry(working: int, blocked: int = 0, unpromoted: int = 0) -> dict[str, int]:
    return {"working": working, "blocked": blocked, "unpromoted": unpromoted}


def _row(project: str, session: str, entry: dict[str, int]) -> dict:
    """A transition-shaped row for one project, carrying an explicit partition.

    The project holds only this session's runs, so the row's own project totals
    equal the session's entry -- which is what a sessionless follower renders and
    what the negative control leaves in place of the session-wide figure.
    """
    event = recovery._watch_transition(
        project,
        kind="transition",
        snapshot={
            "run_id": f"r-{project}",
            "node": f"n-{project}   ",
            "session": session,
            "role": "implement",
        },
        previous="dispatched",
        current="working",
        counts={"working": 0, "blocked": 0, "unpromoted": 0, "sessions": {}},
        spend_runs=[],
        rate_statuses={},
    )
    event["session_counts"] = {"": _entry(0), session: dict(entry)}
    event["working"] = int(entry.get("working") or 0)
    event["blocked"] = int(entry.get("blocked") or 0)
    event["unpromoted"] = int(entry.get("unpromoted") or 0)
    return event


def _append(project: str, event: dict) -> None:
    """Append one transition to a project's watch stream, as a producer does."""
    path = runs.watch_stream_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _render(event: dict, session: str | None = None) -> str:
    if session is None:
        return recovery.format_watch_transition(event)
    return recovery.format_watch_transition(event, session=session)


def _publish(
    project: str,
    session: str,
    *,
    age_seconds: float = 0.0,
    code_stamp: str | None = None,
) -> Path:
    """Publish this process as one project's live producer, as its watcher does.

    The record is the snapshot document the obligation reader classifies: a live
    local pid with its start tick, the running code's stamp, and a publication
    time. Nothing else decides whether another project's figure counts.
    """
    pid = os.getpid()
    moment = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    document = {
        "computed_at": moment.isoformat().replace("+00:00", "Z"),
        "producer": {
            "pid": pid,
            "pid_start_time": obligation_snapshot.process_start_time(pid),
            "code_stamp": code_stamp or obligation_snapshot.source_code_stamp(),
        },
    }
    path = obligation_snapshot.snapshot_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _withdraw(project: str, session: str) -> None:
    """Remove a project's published producer, as a watcher that stopped leaves it."""
    obligation_snapshot.snapshot_path(project, session).unlink(missing_ok=True)


def test_a_scoped_pane_sums_the_session_across_every_project(home) -> None:
    # Project A holds this session at 7 working / 2 blocked / 3 unpromoted, B at
    # 1, and C at 5 on an older transition with another session's on the newest,
    # so C must add nothing.
    _append("A", _row("A", SESSION, _entry(7, 2, 3)))
    _append("B", _row("B", SESSION, _entry(1)))
    _append("C", _row("C", SESSION, _entry(5)))
    _append("C", _row("C", PEER_SESSION, _entry(4)))
    _publish("A", SESSION)
    _publish("B", SESSION)
    # C's producer is live too, so the liveness reading counts it: the only thing
    # that can keep C's older entry of 5 out of the total is which transition the
    # selection returns. Were C left unpublished, the liveness gate would exclude
    # it whatever the selection chose, and the C clause could not fail on it.
    _publish("C", SESSION)

    b_row = _row("B", SESSION, _entry(1))
    a_row = _row("A", SESSION, _entry(7, 2, 3))

    # A row from either project renders the session's total across both, not
    # whichever project the row came from: 7 + 1 working, 2 blocked, 3
    # unpromoted.
    assert " 8w  2h  3d" in _render(b_row, SESSION), _render(b_row, SESSION)
    assert " 8w  2h  3d" in _render(a_row, SESSION), _render(a_row, SESSION)

    # C's newest transition carries no entry for this session: its older entry
    # of 5 must not be read, so the total stays 8 rather than 13. C's producer is
    # live, so only the newest-transition selection keeps the 5 out.
    scoped_a = recovery._scoped_watch_event(a_row, SESSION)
    assert scoped_a["working"] == 8, scoped_a
    assert scoped_a["blocked"] == 2, scoped_a
    assert scoped_a["unpromoted"] == 3, scoped_a

    # The JSON event keeps the row's own project's figures beside the
    # session-wide ones, so a consumer of either reading loses nothing. These
    # are literals, not the row's own entry read back, so a scoped event that
    # dropped the row's figures for the total cannot satisfy them.
    assert scoped_a["project_working"] == 7, scoped_a
    assert scoped_a["project_blocked"] == 2, scoped_a
    assert scoped_a["project_unpromoted"] == 3, scoped_a
    scoped_b = recovery._scoped_watch_event(b_row, SESSION)
    assert scoped_b["working"] == 8, scoped_b
    assert scoped_b["project_working"] == 1, scoped_b
    assert scoped_b["project_blocked"] == 0, scoped_b
    assert scoped_b["project_unpromoted"] == 0, scoped_b

    # A follower with no session is one project's pane. B's own count is 1, and
    # the unscoped render keeps it rather than summing across projects.
    unscoped = _render(b_row)
    assert " 1w  0h  0d" in unscoped, unscoped
    assert " 8w" not in unscoped, unscoped

    # B's watcher stops. Its last transition stays in the events file, but a
    # project with no live producer contributes nothing, so an A row renders
    # A's own figure alone rather than B's stale one.
    _withdraw("B", SESSION)
    assert " 7w  2h  3d" in _render(a_row, SESSION), _render(a_row, SESSION)

    # A quiet but live producer still counts: an aged publication with a live
    # pid is a producer that has not spoken, not one that has gone.
    _publish("B", SESSION, age_seconds=600)
    assert " 8w  2h  3d" in _render(a_row, SESSION), _render(a_row, SESSION)

    # So does a producer running source other than this checkout.
    _publish("B", SESSION, code_stamp="other-code")
    assert " 8w  2h  3d" in _render(a_row, SESSION), _render(a_row, SESSION)

    # A B transition takes this session's B count to zero: the next A row drops
    # to 7, so the total follows the session's live work rather than holding the
    # largest figure ever seen.
    _publish("B", SESSION)
    _append("B", _row("B", SESSION, _entry(0)))
    assert " 7w  2h  3d" in _render(a_row, SESSION), _render(a_row, SESSION)
