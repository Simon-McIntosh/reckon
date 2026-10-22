"""A reload's replayed baseline prints no row where the pane already shows it.

A producer reload replays every live run as a fresh baseline row, so each run's
row returns to the pane after every reload. Where the row names the word the
pane already draws, and carries the same duty beside it, the row is inventory
rather than news: the grid takes its left half from the pane's memory, so it
renders as a self-transition — ``waiting -> waiting`` — carrying only a growing
elapsed. A run standing in ``waiting`` declares the external condition it waits
on, and that declaration is what carries such a row past the row policy's
same-word rule, which is why the replay reached the pane at all.

The duty is compared alongside the word because the live row path keeps a
same-word row whose declaration the coordinator must be able to read, so a
replay naming a changed condition is news and still prints.

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
HORIZON = 3600


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


class _RecordingPath(ticker.PaneRowPath):
    """A row path that also records the rows the replay handed it.

    What the pane receives is the row policy's answer as well as the replay's,
    so a replication the policy happens to withhold leaves no trace in the rows
    the pane sees. Recording what the replay itself delivers lets a case ask
    about the replay's own decision instead.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fed: list[dict] = []

    def feed(self, event, *, now: float):
        if event is not None:
            self.fed.append(dict(event))
        return super().feed(event, now=now)


def _replay(tmp_path: Path, events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Feed one recorded stream through a follower's own row path.

    Returns the rows the pane received and the rows the replay itself delivered
    for the row path to judge.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text("".join(f"{json.dumps(row)}\n" for row in events))
    ticks = itertools.count(0.0, 40.0)
    path = _RecordingPath(own_session=SESSION, observed_sessions={SESSION}, reported={})
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
    return rows + list(path.flush(now=float("inf"))), path.fed


def test_a_replay_of_the_pane_shown_state_draws_no_row(tmp_path) -> None:
    """The run is drawn twice: its first sighting and the real change.

    The stream holds the run's move into ``waiting``, a reload's replayed
    ``new -> waiting`` baseline for it declaring the same condition, then a real
    ``waiting -> working``. The baseline re-announces what the pane already
    holds, so it draws nothing; without the suppression the same replay draws
    three rows for the run.
    """
    rows, _fed = _replay(
        tmp_path,
        [
            _event(
                "transition",
                previous="working",
                state="waiting",
                observed_at="2026-10-21T03:41:58Z",
                expected_horizon_seconds=HORIZON,
            ),
            _event(
                "baseline",
                previous=None,
                state="waiting",
                observed_at="2026-10-21T07:20:00Z",
                expected_horizon_seconds=HORIZON,
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
    rows, _fed = _replay(
        tmp_path,
        [
            _event(
                "transition",
                previous="working",
                state="waiting",
                observed_at="2026-10-21T03:41:58Z",
                expected_horizon_seconds=HORIZON,
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


def test_a_replay_naming_a_changed_duty_still_prints(tmp_path) -> None:
    """The declaration the pane was shown is replayed only when it is unchanged.

    A run waiting on the same word with a different condition is a different
    situation for the coordinator reading it, so the replay of that condition is
    news: the row prints, exactly as a same-word row prints on the live path.
    """
    rows, _fed = _replay(
        tmp_path,
        [
            _event(
                "transition",
                previous="working",
                state="waiting",
                observed_at="2026-10-21T03:41:58Z",
                expected_horizon_seconds=HORIZON,
            ),
            _event(
                "baseline",
                previous=None,
                state="waiting",
                observed_at="2026-10-21T07:20:00Z",
                expected_horizon_seconds=HORIZON * 2,
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
    assert replayed, f"a replay of a changed condition is news and prints; got {rows!r}"
    assert replayed[0].get("expected_horizon_seconds") == HORIZON * 2, replayed


def test_a_baseline_that_printed_is_remembered_for_the_next_replay(tmp_path) -> None:
    """A replay's own delivery is the memory the next replay is judged against.

    Two replays of one run can arrive inside a single arming. The first names a
    state the pane has not drawn, so it is left to the replay's row path, which
    draws it; the second repeats that state, so it is inventory and draws
    nothing. Judging the second against the pane's memory alone would leave it
    to the row path as well, so the count of rows the replay delivers is what
    this case reads: the row path withholds the second one for its own reason,
    and the replay must not depend on that.
    """
    rows, fed = _replay(
        tmp_path,
        [
            _event(
                "transition",
                previous="working",
                state="waiting",
                observed_at="2026-10-21T03:41:58Z",
                expected_horizon_seconds=HORIZON,
            ),
            _event(
                "baseline",
                previous=None,
                state="blocked",
                observed_at="2026-10-21T07:20:00Z",
            ),
            _event(
                "baseline",
                previous=None,
                state="blocked",
                observed_at="2026-10-21T07:24:00Z",
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
    assert len(replayed) == 1, (
        f"the second replay of a state the pane already shows is inventory; got {rows!r}"
    )
    delivered = [row for row in fed if row.get("event") == "baseline"]
    assert len(delivered) == 1, (
        "the replay drops the second baseline itself rather than leaving it to "
        f"the row path to withhold; it delivered {delivered!r}"
    )
