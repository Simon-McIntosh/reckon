"""The follower delivers a settled row when it asks for action, and only then.

Section 3 of *a-follower-row-is-worth-a-wake*: the follower runs the
coordinator's pane through the row policy, so a run's start, the bare
re-announcement of a state the pane already shows, and the terminal echo of a
completion the coordinator's own command caused all stay off the pane. What
prints is the settled change that asks the coordinator for something -- a
completion to record, a block to repair -- and a completion no later
transition supersedes still prints on its own, released by the wait pass
rather than by a later row that may never come.

These drive the follower's own generator against a synthetic config home and a
real stream, because a policy that answers correctly when fed directly can
still be wired so that nothing reaches it. The clock is the test's own, so the
settle window is an exact number and a release is measured against the instant
its row was fed, not against how fast the host happened to run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import runs, ticker

PROJECT = "settled-proj"
SESSION = "s1"
RUN_A = "r-settled-a"
RUN_B = "r-settled-b"

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})

# The wait pass's own step. The arming advances its clock by this much per
# sleep, so the settle window's release lands on an exact pass.
POLL = 0.5
# Long enough that the arming reaches the wait pass that flushes the held row
# and then ends by its own deadline, without waiting for the real settle window.
LIFETIME = ticker.SETTLE_WINDOW + 3 * POLL


class _Clock:
    """A monotonic stand-in the arming advances only when it sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.start = start
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests, streams and checkpoints in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def observer_pane(monkeypatch):
    """Build the follower's row path with observer context shown.

    The coordinator's pane holds observer rows back; this is the mutation the
    negative control applies, and the flag ``PaneRowPath`` already carries for
    exactly that comparison.
    """
    original = ticker.PaneRowPath

    class _Showing(original):
        def __init__(self, **kwargs):
            kwargs["show_observer"] = True
            super().__init__(**kwargs)

    monkeypatch.setattr(ticker, "PaneRowPath", _Showing)
    return _Showing


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _live_runs(home: Path, *run_ids: str) -> None:
    for run_id in run_ids:
        _write_pointer(home, run_id, f"node-{run_id}", phase="working")


def _event(
    run_id: str,
    node: str,
    *,
    state: str,
    observed_at: str,
    previous: str | None = None,
) -> dict:
    return {
        "project": PROJECT,
        "event": "transition",
        "run_id": run_id,
        "node": node,
        "session": SESSION,
        "from_state": previous,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": observed_at,
        "legacy": False,
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    with stream_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(f"{json.dumps(event)}\n")


def _arm(clock: _Clock, **kwargs) -> list[tuple[float, dict]]:
    """Run one arming to its own deadline and time each fleet row it drew.

    The clock is injected and advanced one ``POLL`` per wait pass, so a row's
    release instant is measured against the clock the arming fed its rows on
    rather than against how long the host took to run it.
    """
    generator = cli._follow_watch_lines(
        PROJECT,
        session=SESSION,
        poll_interval=POLL,
        sleeper=clock.advance,
        sweep=None,
        clock=clock,
        lifetime=LIFETIME,
        **kwargs,
    )
    return [
        (clock(), event) for event in generator if event.get("event") in _FLEET_EVENTS
    ]


def _fixture_events() -> list[dict]:
    """The two-run fixture: a start-to-record, and a block two seconds later.

    The first run opens in ``dispatched -> working`` and reaches ``recorded``
    through ``complete`` -- a start the dispatch call already reported, a
    completion the coordinator owes, and a terminal echo that supersedes
    nothing. The second run reaches ``blocked`` through ``exited-unfinished``
    two seconds after it left ``working``.
    """
    base = datetime.now(UTC).timestamp()
    return [
        _event(RUN_A, "node-a", state="dispatched", observed_at=_iso(base)),
        _event(
            RUN_A,
            "node-a",
            state="working",
            observed_at=_iso(base + 1.0),
            previous="dispatched",
        ),
        _event(
            RUN_A,
            "node-a",
            state="complete",
            observed_at=_iso(base + 2.0),
            previous="working",
        ),
        _event(
            RUN_A,
            "node-a",
            state="recorded",
            observed_at=_iso(base + 3.0),
            previous="complete",
        ),
        _event(RUN_B, "node-b", state="working", observed_at=_iso(base)),
        _event(
            RUN_B,
            "node-b",
            state="exited-unfinished",
            observed_at=_iso(base + 4.0),
            previous="working",
        ),
        _event(
            RUN_B,
            "node-b",
            state="blocked",
            observed_at=_iso(base + 6.0),
            previous="exited-unfinished",
        ),
    ]


def _states(rows: list[tuple[float, dict]]) -> list[tuple[object, object]]:
    return [(row["from_state"], row["to_state"]) for _, row in rows]


def _deliver(
    home: Path,
) -> tuple[list[tuple[float, dict]], list[tuple[float, dict]], float]:
    """Arm once to place the stream, feed the fixture, arm and collect rows.

    Returns the rows the first arming drew, the rows the pane received from the
    fixture, each with the clock it was released on, and the instant the
    fixture was fed -- the second arming's own start, before its first sleep.
    """
    _live_runs(home, RUN_A, RUN_B)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)
        # A first arming establishes the follower's place and draws the fleet
        # baseline, which for a working run is a re-announcement the coordinator
        # pane holds back -- so it wakes nobody.
        first = _arm(_Clock())

        _append_stream(stream_path, _fixture_events())
        clock = _Clock()
        fed_at = clock.start
        rows = _arm(clock)
    return first, rows, fed_at


def test_the_pane_receives_only_the_settled_changes(home) -> None:
    """Two runs deliver exactly two rows: the completion and the block.

    The start (``dispatched -> working``), the bare ``working`` and the
    terminal echo (``complete -> recorded``) ask for nothing, so they are held
    back; the completion and the block are the news the coordinator acts on.
    """
    first, rows, _ = _deliver(home)
    # The attach's own re-announcements ask for nothing, so the first arming
    # wakes nobody.
    assert first == [], first
    assert len(rows) == 2, rows
    assert _states(rows) == [
        ("working", "complete"),
        ("exited-unfinished", "blocked"),
    ], rows


def test_a_completion_prints_on_its_own_within_the_settle_window(home) -> None:
    """A completion no later transition supersedes still reaches the pane.

    The terminal echo does not supersede the held completion, so nothing later
    releases it -- the wait pass does, once the settle window elapses. Release
    therefore lands within one poll interval of the window's close, with no
    later row to carry it.
    """
    _, rows, fed_at = _deliver(home)
    completion = [(at, row) for at, row in rows if row["to_state"] == "complete"]
    assert len(completion) == 1, rows
    released_at = completion[0][0]
    elapsed = released_at - fed_at
    assert ticker.SETTLE_WINDOW <= elapsed <= ticker.SETTLE_WINDOW + POLL, (
        fed_at,
        released_at,
    )


def test_observer_context_shown_delivers_the_start_rows_too(
    home, observer_pane
) -> None:
    """Negative control: the pane built with observer context shown prints more.

    This is the mutation the node's gate fails against. With the follower's row
    path built to show observer context, the start and the bare working rows
    reach the pane, so the two-row assertion above cannot hold -- proving that
    assertion is about the hold and not about a follower that reads nothing.
    """
    _, rows, _ = _deliver(home)
    assert len(rows) != 2, rows
    assert ("dispatched", "working") in _states(rows), rows
    assert (None, "working") in _states(rows), rows
