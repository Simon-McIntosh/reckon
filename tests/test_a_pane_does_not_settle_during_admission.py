"""A pane holds its settle across an admission handover.

An admission pass adopts a session's queued request by deleting that request's
the pointer and then dispatching, and the admitted run's worktree is built
before its own pointer is written. For those seconds the session holds no
unfinished run at all, so a settle judgement that ended on its first empty
reading left the pane in the middle of the handover: the admitted run started,
ran to completion, and the pane drew none of it.

The hold is therefore a bound on the handover rather than on a poll. The pane
ends only once its population has held no unfinished run for the whole grace,
and a run queued or admitted inside it keeps the pane. A request still queued
is unfinished work in its own right, so a session waiting its turn never
settles either.

These cases drive the follower's own generator on an injected clock and
sleeper, so the grace is an exact number of fake seconds and a settle is
measured against the moment the population drained rather than against however
fast the host happened to run.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew
from reckon import crew_follow_commands as follow_commands
from reckon.crew import follow_checkpoint, runs

PROJECT = "admission-grace-proj"
SESSION = "s-admission"
RUN_QUEUED = "r-awaiting-admission"
RUN_MINE = "r-mine"
RUN_ADMITTED = "r-admitted"

# The grace the cases drive: the module's own named constant, so the hold
# under test is the one a real follower runs with and shortening it is the
# mutation the negative control applies.
GRACE = follow_commands.FOLLOWER_SETTLE_GRACE_SECONDS
# The wait pass's step, real time only: the case's own clock never advances
# with it, so an empty window lasts exactly as long as the case says.
POLL = 0.005

_FLEET_EVENTS = frozenset({"baseline", "transition", "manifest-rewritten"})


class _Clock:
    """A monotonic stand-in the case advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Wall:
    """A wall clock the case sets, so a printed idle line's stamp is exact.

    Everything but ``time`` is the real module's, so the line's own
    ``strftime``/``localtime`` formatting is the production one.
    """

    def __init__(self, value: float) -> None:
        self.value = value

    def time(self) -> float:
        return self.value

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # The grace these cases drive is the module's own, so a caller's override
    # in the environment this file happens to run under cannot shorten it.
    monkeypatch.delenv(follow_commands.FOLLOWER_SETTLE_GRACE_ENV, raising=False)
    return config_home


def _write_pointer(home: Path, run_id: str, *, phase: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": f"node-{run_id}", "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _drop_pointer(run_id: str) -> None:
    """Delete a request's pointer, as the admission pass does on adoption."""
    crew.pointer_path(run_id).unlink()


def _event(run_id: str, node: str, *, state: str, observed_at: str, previous=None):
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


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    with stream_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(f"{json.dumps(event)}\n")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _yield_briefly(_seconds: float) -> None:
    time.sleep(POLL)


class _Arming:
    """One follower generator consumed on a thread, with its own clock."""

    def __init__(self, *, clock: _Clock | None = None):
        self.clock = clock if clock is not None else _Clock()
        self.events: list[dict] = []
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> _Arming:
        # No settle_interval: the grace under test is the module's own default,
        # so shortening the constant is the mutation the control applies.
        generator = follow_commands._follow_watch_lines(
            PROJECT,
            session=SESSION,
            poll_interval=POLL,
            sleeper=_yield_briefly,
            stop=self.stop,
            sweep=None,
            clock=self.clock,
            settle=True,
        )

        def run() -> None:
            for event in generator:
                self.events.append(event)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def end(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(5.0)

    def settles(self) -> list[dict]:
        return [
            e
            for e in self.events
            if e.get("event") == follow_commands.FOLLOWER_SETTLE_EVENT
        ]

    def rows(self, run_id: str) -> list[dict]:
        return [
            e
            for e in self.events
            if e.get("event") in _FLEET_EVENTS and e.get("run_id") == run_id
        ]


def _wait_until(predicate, *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")


def test_the_settle_grace_is_two_minutes() -> None:
    """The hold is one named constant of two minutes.

    It bounds the dispatch an admission pass makes -- the queued request's
    pointer deleted, the admitted run's worktree built, its pointer written --
    and that window is what the pane must survive, so the figure is a bound on
    the handover rather than a poll interval.
    """
    assert follow_commands.FOLLOWER_SETTLE_GRACE_SECONDS == 120.0


def test_a_handover_keeps_the_pane_and_draws_the_admitted_run(home: Path) -> None:
    """A run admitted inside the grace keeps the pane (case 1).

    The session's queued request is present and the pane carries it. The
    request's pointer is then removed with nothing in its place for several
    seconds -- less than the grace -- and the admitted run's pointer and its
    start transition follow. The pane must neither settle nor miss the start.
    """
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        _write_pointer(home, RUN_QUEUED, phase="queued")
        crew.list_live(project=PROJECT)
        arming = _Arming().start()
        try:
            _wait_until(
                lambda: follow_checkpoint.exists(PROJECT, SESSION),
                timeout=5.0,
                description="the arming to attach and record its place",
            )
            # A sample lands on the queued request while it is still there, so
            # the pane has seen work and the drain the grace is measured from
            # is a real one.
            arming.clock.advance(5.0)
            time.sleep(0.3)

            _drop_pointer(RUN_QUEUED)
            # Two empty samples inside the grace: the second is more than a
            # one-second hold after the drain and less than the grace.
            arming.clock.advance(4.0)
            time.sleep(0.2)
            arming.clock.advance(4.5)
            time.sleep(0.3)
            assert not arming.settles(), (
                "the pane settled while an admitted run was still being placed"
            )

            _write_pointer(home, RUN_ADMITTED, phase="working")
            observed = datetime.now(UTC).timestamp()
            _append_stream(
                stream_path,
                [
                    _event(
                        RUN_ADMITTED,
                        f"node-{RUN_ADMITTED}",
                        state="dispatched",
                        observed_at=_iso(observed),
                    ),
                    _event(
                        RUN_ADMITTED,
                        f"node-{RUN_ADMITTED}",
                        state="working",
                        observed_at=_iso(observed + 1.0),
                        previous="dispatched",
                    ),
                ],
            )
            arming.clock.advance(4.0)
            time.sleep(0.5)
            # The admitted run's attempt opens with the launch the pane draws,
            # once: the pulse the pane must not lose is its own liveness,
            # measured by the settle below.
            admitted_rows = arming.rows(RUN_ADMITTED)
            assert [row.get("to_state") for row in admitted_rows] == ["working"], (
                admitted_rows
            )
            assert not arming.settles(), "the pane settled after the admission"
        finally:
            arming.end()

    assert not arming.settles(), arming.settles()


def test_a_population_empty_past_the_grace_settles_once(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty population past the grace settles once, and prints nothing.

    The population is empty for well past the grace, so the pane settles
    exactly once. The fleet drained with nothing outstanding, so the settle row
    prints no line -- it names nothing the coordinator can act on. The row is
    still delivered, which is what a session host retires the child on.
    """
    drain_wall = time.time() - 3 * 3600
    wall = _Wall(drain_wall)
    monkeypatch.setattr(follow_commands, "time", wall)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN_MINE, phase="working")
        crew.list_live(project=PROJECT)
        arming = _Arming().start()
        try:
            _wait_until(
                lambda: follow_checkpoint.exists(PROJECT, SESSION),
                timeout=5.0,
                description="the arming to attach and record its place",
            )
            # A live sample lands while the run is still there, so the pane has
            # seen work and the grace is measured from a drain rather than from
            # a population it never saw hold anything.
            arming.clock.advance(5.0)
            time.sleep(0.3)
            _drop_pointer(RUN_MINE)
            # Move the wall clock well past the sighting: a drained settle
            # prints nothing, so no stamp of either moment can appear.
            wall.value = drain_wall + 2 * 3600

            arming.clock.advance(2.0)
            time.sleep(0.2)
            arming.clock.advance(10.0)
            time.sleep(0.2)
            assert not arming.settles(), "the pane settled before the grace had elapsed"

            arming.clock.advance(GRACE)
            _wait_until(
                arming.settles,
                timeout=5.0,
                description="the pane to settle once the grace elapsed",
            )
            settles = arming.settles()
            assert len(settles) == 1, settles
            # Nothing is outstanding, so the settle row prints no line at all.
            assert settles[0]["line"] == "", settles[0]["line"]
        finally:
            arming.end()


def test_a_queued_request_never_settles_the_pane(home: Path) -> None:
    """A session waiting its turn is unfinished work, so the pane waits too.

    A queued request holds no worker yet, but it is work the pane is owed, and
    a long wait for admission must not end the follower that would draw it.
    """
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _write_pointer(home, RUN_QUEUED, phase="queued")
        crew.list_live(project=PROJECT)
        arming = _Arming().start()
        try:
            _wait_until(
                lambda: follow_checkpoint.exists(PROJECT, SESSION),
                timeout=5.0,
                description="the arming to attach and record its place",
            )
            arming.clock.advance(GRACE)
            time.sleep(0.2)
            for _ in range(6):
                arming.clock.advance(GRACE)
                time.sleep(0.1)
            assert not arming.settles(), "the pane settled while a request was queued"
            assert arming.thread is not None and arming.thread.is_alive(), (
                "the follower left while its session still held a queued request"
            )
        finally:
            arming.end()
