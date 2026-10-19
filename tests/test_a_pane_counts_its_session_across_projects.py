"""A session-scoped follower's row counts that session's work in every project.

Measured 2026-10-18: session ``83d14db1`` worked in power_convertor and reckon
at once, and its pane's worker count swung between 7w and 8w and back within a
minute, because each row drew its own project's figures under one unlabelled
column. One project's watcher cannot see the session's work in another, so the
row's counters must sum the session's entry from every project's events file
under the watch directory.

The fixture drives two projects whose events files each carry the per-session
partition a real producer writes, plus a third whose newest transition carries
no entry for the session.
"""

from __future__ import annotations

import json

from reckon.crew import recovery, runs

SESSION = "83d14db1"


def _entry(working: int, blocked: int = 0, unpromoted: int = 0) -> dict[str, int]:
    return {"working": working, "blocked": blocked, "unpromoted": unpromoted}


def _row(project: str, session: str, entry: dict[str, int]) -> dict:
    """A transition-shaped row for one project, carrying an explicit partition.

    The project holds only this session's runs, so the row's own project totals
    equal the session's entry — which is what a sessionless follower renders and
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


def test_a_scoped_pane_sums_the_session_across_every_project(home) -> None:
    # Project A holds this session at 7 working, B at 1, C at 5 for another
    # session. A third project's newest transition then leaves out this
    # session, so it must add nothing.
    _append("A", _row("A", SESSION, _entry(7)))
    _append("B", _row("B", SESSION, _entry(1)))
    _append("C", _row("C", SESSION, _entry(5)))
    _append("C", _row("C", "peer-session", _entry(4)))

    b_row = _row("B", SESSION, _entry(1))
    a_row = _row("A", SESSION, _entry(7))

    # A row from either project renders the session's total across both, not
    # whichever project the row came from.
    assert " 8w  0h  0d" in _render(b_row, SESSION), _render(b_row, SESSION)
    assert " 8w  0h  0d" in _render(a_row, SESSION), _render(a_row, SESSION)

    # C's newest transition carries no entry for this session: its older entry
    # of 5 must not be read, so the total stays 8 rather than 13.
    scoped_a = recovery._scoped_watch_event(a_row, SESSION)
    assert scoped_a["working"] == 8, scoped_a

    # The JSON event keeps the row's own project's figures beside the
    # session-wide ones, so a consumer of either reading loses nothing.
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

    # A B transition takes this session's B count to zero: the next A row drops
    # to 7, so the total follows the session's live work rather than holding the
    # largest figure ever seen.
    _append("B", _row("B", SESSION, _entry(0)))
    assert " 7w  0h  0d" in _render(a_row, SESSION), _render(a_row, SESSION)
