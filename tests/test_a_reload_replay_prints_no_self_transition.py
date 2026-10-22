"""A reload's replayed baseline prints no row where the pane already shows it.

A producer reload replays every live run as a fresh baseline row, so each run's
row returns to the pane after every reload. Where the row's destination is the
state the pane last showed for that run the row is inventory rather than news:
the grid draws the left half from its memory of what it showed, so the row
renders as a self-transition — ``waiting -> waiting`` — carrying only a growing
elapsed. A run standing in ``waiting`` declares the external condition it waits
on, and that declaration is what carries such a row past the row policy's
same-word rule, which is why the replay reached the pane at all.

Each case drives the follower's own fleet replay over a recorded stream, so the
rows counted are the rows the pane is handed rather than a second
implementation's opinion of them.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from reckon.crew import ticker
from reckon.crew_follow_commands import _fleet_replay

PROJECT = "reload-replay-proj"
SESSION = "s-reload"
RUN = "r-reload-a"
NODE = "reload-node-a"


def _event(kind, *, previous, state, observed_at, **extra):
    row = {
        "project": PROJECT,
        "event": kind,
        "run_id": RUN,
        "node": NODE,
        "session": SESSION,
        "from_state": previous,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": observed_at,
        "legacy": False,
    }
    row.update(extra)
    return row


def _replay(tmp_path: Path, events: list[dict]) -> list[dict]:
    """Feed one recorded stream through a follower's own row path."""
    stream = tmp_path / "stream.jsonl"
    stream.write_text("".join(f"{json.dumps(row)}\n" for row in events))
    ticks = itertools.count(0.0, 40.0)
    path = ticker.PaneRowPath(
        own_session=SESSION, observed_sessions={SESSION}, reported={}
    )
    rows = _fleet_replay(
        path,
        baseline=[],
        stream_path=stream,
        clock=lambda: next(ticks),
        reannounce=True,
        resume_from=0,
        boundary=stream.stat().st_size,
    )
    # The arming's read loop keeps feeding the path after the replay, so a row
    # the policy held for its own window is released rather than lost.
    return rows + list(path.flush(now=float("inf")))


def test_a_replay_of_the_pane_shown_state_draws_no_row(tmp_path) -> None:
    """The run is drawn twice: its first sighting and the real change.

    The stream holds the run's move into ``waiting``, a reload's replayed
    ``new -> waiting`` baseline for it, then a real ``waiting -> working``. The
    baseline re-announces the state the pane already shows, so it draws nothing;
    without the suppression the same replay draws three rows for the run.
    """
    rows = _replay(
        tmp_path,
        [
            _event(
                "transition",
                previous="working",
                state="waiting",
                observed_at="2026-10-21T03:41:58Z",
            ),
            _event(
                "baseline",
                previous=None,
                state="waiting",
                observed_at="2026-10-21T07:20:00Z",
                expected_horizon_seconds=3600,
            ),
            _event(
                "transition",
                previous="waiting",
                state="working",
                observed_at="2026-10-21T07:25:00Z",
            ),
        ],
    )

    assert [row.get("to_state") for row in rows] == ["waiting", "working"], (
        f"the run is drawn for its first sighting and its real change only; got {rows!r}"
    )
    assert not any(row.get("event") == "baseline" for row in rows), (
        f"the replay of the state the pane already shows is inventory; got {rows!r}"
    )


def test_a_replay_naming_another_state_still_prints(tmp_path) -> None:
    """A baseline whose destination the pane has not shown is news and prints."""
    rows = _replay(
        tmp_path,
        [
            _event(
                "transition",
                previous="working",
                state="waiting",
                observed_at="2026-10-21T03:41:58Z",
            ),
            _event(
                "baseline",
                previous=None,
                state="blocked",
                observed_at="2026-10-21T07:20:00Z",
            ),
            _event(
                "transition",
                previous="waiting",
                state="working",
                observed_at="2026-10-21T07:25:00Z",
            ),
        ],
    )

    replayed = [row for row in rows if row.get("event") == "baseline"]
    assert replayed, (
        f"a baseline naming a state the pane has not shown prints; got {rows!r}"
    )
    assert replayed[0].get("to_state") == "blocked", replayed
