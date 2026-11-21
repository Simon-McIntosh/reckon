"""A pane receives a project's rows only while its session holds work there.

A dispatch asks the session's host for a follower before the dispatch that
requested it has been admitted, so a request that is refused and stores nothing
can leave a follower attached to a project its session holds nothing in. That
follower then paints the project's producer-health rows: each one wakes the
coordinator over a producer it does not own, and a lost row tells it to re-arm a
watch for a project that is not theirs.

The cases below are read through the follower's own generator, so the rows
measured are the rows a pane draws:

* a session whose dispatch into a second project is refused receives no later
  row for that project;
* a session holding a run in a second project receives that project's rows, and
  stops receiving them once the run is retired and nothing else of its own
  remains there;
* a lost row's text names no command for a project in which the session holds
  nothing, and the same row delivered to a session that does hold work there
  names that project's command, which is what keeps an inspected absence from
  being an empty result.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest

from reckon import crew
from reckon import crew_follow_commands as follow
from reckon.crew import obligation_snapshot, runs

PROJECT_WORKED = "pane-worked-project"
PROJECT_OTHER = "pane-other-project"
SESSION = "s-pane"
INSTANT = datetime(2026, 11, 1, tzinfo=UTC)
BOUND = obligation_snapshot.FRESHNESS_WINDOW_SECONDS
SAMPLE = obligation_snapshot.STAT_IDENTITY_INTERVAL_SECONDS


class Clock:
    """A monotonic reading a test advances one sample at a time."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture()
def clock(tmp_path, monkeypatch) -> Clock:
    """A crew home of its own, and an obligation clock the test drives."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(runs, "renew_producer_lease", lambda *args: None)
    monkeypatch.setattr(
        runs,
        "follower_owner",
        lambda: (os.getpid(), runs._process_start_time(os.getpid())),
    )
    clock = Clock()
    monkeypatch.setattr(
        obligation_snapshot, "_utc_now", lambda: INSTANT + timedelta(seconds=clock())
    )
    return clock


def _publish(project: str, age: int, clock: Clock) -> None:
    """Write this project's publication document, aged by ``age`` seconds."""
    document = {"computed_at": (INSTANT + timedelta(seconds=clock() - age)).isoformat()}
    obligation_snapshot.write_snapshot(project, SESSION, document)


def _write_run(run_id: str, *, project: str, session: str, phase: str) -> None:
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "session": session,
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": "2026-11-01T00:00:00Z",
            "process_alive": None,
        },
    )


def _retire(run_id: str) -> None:
    crew.pointer_path(run_id).unlink(missing_ok=True)


def _drive(project: str, clock: Clock, monkeypatch, *, samples: int = 4, at=None):
    """Run the follower's watch stream against this project for a few samples."""
    stop = threading.Event()
    monkeypatch.setattr(runs, "producer_live", lambda name: True)
    stream = runs.watch_stream_path(project)
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text("")
    monkeypatch.setattr(
        runs,
        "watch_stream_cursor",
        lambda name: {
            "producer": {},
            "stream_path": str(stream),
            "baseline": [],
            "offset": 0,
        },
    )

    def sleep(_interval: float) -> None:
        clock.value += SAMPLE
        if at is not None:
            at(clock())
        if clock() >= samples * SAMPLE:
            stop.set()

    return [
        (clock(), event)
        for event in follow._follow_watch_lines(
            project,
            session=SESSION,
            subscriber=None,
            clock=clock,
            sleeper=sleep,
            poll_interval=SAMPLE,
            stop=stop,
            sweep=None,
        )
    ]


def _producer_rows(received) -> list[dict]:
    return [
        event
        for _, event in received
        if str(event.get("event") or "").startswith("producer-")
    ]


def test_a_refused_dispatch_leaves_rows_out(clock, monkeypatch):
    """A session holding nothing in the project receives none of its rows."""
    _publish(PROJECT_OTHER, int(BOUND + 60), clock)
    received = _drive(PROJECT_OTHER, clock, monkeypatch)
    assert _producer_rows(received) == []


def test_the_same_rows_flow_while_the_session_holds_a_run(clock, monkeypatch):
    """The positive control: a run here earns the rows the case above withholds."""
    _publish(PROJECT_WORKED, int(BOUND + 60), clock)
    _write_run("r-worked", project=PROJECT_WORKED, session=SESSION, phase="working")
    received = _drive(PROJECT_WORKED, clock, monkeypatch)
    rows = _producer_rows(received)
    assert [row["event"] for row in rows] == ["producer-lost"], rows


def test_rows_stop_once_the_run_is_retired(clock, monkeypatch):
    """The pane's rows end with the last run its session holds there."""
    _publish(PROJECT_WORKED, int(BOUND + 60), clock)
    _write_run("r-worked", project=PROJECT_WORKED, session=SESSION, phase="working")

    def retire_and_recover(moment: float) -> None:
        if moment == 2 * SAMPLE:
            _retire("r-worked")
            _publish(PROJECT_WORKED, 0, clock)

    received = _drive(PROJECT_WORKED, clock, monkeypatch, at=retire_and_recover)
    assert [row["event"] for row in _producer_rows(received)] == ["producer-lost"]


def test_a_lost_rows_text_names_no_command_for_a_project_held_nothing_in(
    clock, monkeypatch
):
    """No command for a project the session holds nothing in reaches its pane."""
    remedy = runs.watcher_ensure_line(PROJECT_OTHER)
    _publish(PROJECT_OTHER, int(BOUND + 60), clock)
    received = _drive(PROJECT_OTHER, clock, monkeypatch)
    drawn = [event for _, event in received]
    assert all(PROJECT_OTHER not in str(row.get("line") or "") for row in drawn)
    assert all(remedy not in str(row.get("line") or "") for row in drawn)


def test_a_lost_row_names_the_command_for_the_project_it_is_about(clock, monkeypatch):
    """The row a watched project earns still tells its reader how to re-arm it."""
    remedy = runs.watcher_ensure_line(PROJECT_WORKED)
    _publish(PROJECT_WORKED, int(BOUND + 60), clock)
    _write_run("r-worked", project=PROJECT_WORKED, session=SESSION, phase="working")
    received = _drive(PROJECT_WORKED, clock, monkeypatch)
    lost = [row for row in _producer_rows(received) if row["event"] == "producer-lost"]
    assert lost, received
    assert lost[0]["remedy"] == remedy
    assert remedy in lost[0]["line"]
