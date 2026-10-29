"""The follower's settle row prints nothing and is still emitted.

A pane goes dark exactly when a coordinator looks up from it. The row that
leaves behind is how the follower's host retires the child, so it is emitted
whatever the fleet still owes -- but its line is empty, because the runs a
drained session owes are named, with the command for each, by the obligations
list rather than by a sentence composed for the pane.

Two facts decide the shape of the cases below.

The settle is hinged on the pane having watched its fleet live, because a
dispatch arms a follower before it publishes the run's pointer: a follower armed
into an already quiet session keeps following. So every drain here is reached
from a run the pane saw working.

The set is the owning session's runs alone. A peer session's working run in the
same fixture is work this pane was never asked to watch, so it neither holds the
pane open nor appears in the derivation.
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
from reckon.crew import needs_you as needs_you_module

PROJECT = "drain-proj"
SESSION = "drain-owner"
PEER = "drain-peer"
RUN_MINE = "r-mine"
RUN_PEER = "r-peer"

# The grace the cases drive: the module's own named constant, so the hold under
# test is the one a real follower runs with, shortened below to a value a case
# can wait out.
GRACE = 0.5
# The wait pass's step, real time only: the cases' own clock never advances with
# it, so an empty window lasts exactly as long as the case says.
POLL = 0.005
# How long a case waits for the pane to drain. The pane is a thread on a machine
# carrying a whole test fleet when this runs, so a bound tight enough to trip on
# a loaded worker measures the worker and not the pane.
DRAIN_BOUND = 20.0


class _Clock:
    """A monotonic stand-in the case advances by hand."""

    def __init__(self, start: float) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # The grace these cases drive is the module's own, so a caller's override in
    # the environment this file happens to run under cannot shorten it.
    monkeypatch.delenv(follow_commands.FOLLOWER_SETTLE_GRACE_ENV, raising=False)
    return config_home


def _stamp_seconds_ago(seconds: float) -> str:
    """A UTC stamp this many seconds before now, in the stream's own spelling."""
    moment = datetime.fromtimestamp(time.time() - seconds, tz=UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_pointer(
    home: Path,
    run_id: str,
    *,
    session: str,
    phase: str,
    states: list[tuple[str, str]],
    manifest_status: str,
) -> None:
    """Write one live pointer with the stream, manifest and phase a dispatch gives it."""
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "turn.started"})]
    lines.extend(
        json.dumps(
            {
                "run_id": run_id,
                "to_state": state,
                "observed_at": stamp,
                "legacy": False,
            }
        )
        for state, stamp in states
    )
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: {manifest_status}\nblockers: none\n",
        encoding="utf-8",
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(manifest),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _run_working(home: Path, run_id: str, *, session: str) -> None:
    """Put one run in the fleet, working: live work the pane owes a row."""
    _write_pointer(
        home,
        run_id,
        session=session,
        phase="working",
        states=[("working", _stamp_seconds_ago(120))],
        manifest_status="running",
    )


def _run_unpromoted(home: Path, run_id: str, *, session: str) -> None:
    """Deliver one run: it ends, and nothing has promoted it yet."""
    _write_pointer(
        home,
        run_id,
        session=session,
        phase="complete",
        states=[
            ("working", _stamp_seconds_ago(300)),
            ("completed_unpromoted", _stamp_seconds_ago(60)),
        ],
        manifest_status="complete",
    )


def _promote(run_id: str) -> None:
    """Land a run: promotion unlinks the pointer and the session it captured."""
    crew.pointer_path(run_id).unlink()


def _wait_until(predicate, *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")


class _Arming:
    """One follower generator consumed on a thread, with its own clock."""

    def __init__(self, clock: _Clock):
        self.clock = clock
        self.events: list[dict] = []
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> _Arming:
        # No settle_interval: the grace under test is the module's own default,
        # so shortening the module's constant is the mutation the control makes.
        generator = follow_commands._follow_watch_lines(
            PROJECT,
            session=SESSION,
            poll_interval=POLL,
            sleeper=lambda _seconds: time.sleep(POLL),
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
            event
            for event in self.events
            if event.get("event") == follow_commands.FOLLOWER_SETTLE_EVENT
        ]


def _drain(*, clock: _Clock, then) -> list[dict]:
    """Arm one follower over the run live, apply ``then``, and let it drain.

    The run is already live when this is called, so the pane has work to watch:
    that sighting is the hinge the settle is judged on.
    """
    arming = _Arming(clock).start()
    try:
        _wait_until(
            lambda: follow_checkpoint.exists(PROJECT, SESSION),
            timeout=5.0,
            description="the arming to attach and record its place",
        )
        arming.clock.advance(5.0)
        time.sleep(0.5)
        then()
        arming.clock.advance(2.0)
        time.sleep(0.3)
        arming.clock.advance(GRACE)
        _wait_until(
            arming.settles,
            timeout=DRAIN_BOUND,
            description="the pane to drain once the grace elapsed",
        )
        return list(arming.settles())
    finally:
        arming.end()


def test_a_drain_with_an_outstanding_run_prints_no_line(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drained pane whose session still owes a run says nothing, and exits.

    The owning session's last working run ends unpromoted: the pane has no live
    work left to watch, and the run is still owed the coordinator. The row it
    leaves behind is emitted for the host and carries no line naming it. A peer
    session's working run sits in the same fixture, unobserved, and neither
    holds the pane open nor appears in the derivation.
    """
    monkeypatch.setattr(follow_commands, "FOLLOWER_SETTLE_GRACE_SECONDS", GRACE)
    monkeypatch.setattr(follow_commands, "FOLLOWER_SETTLE_SAMPLE_SECONDS", GRACE)
    clock = _Clock(2000.0)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _run_working(home, RUN_MINE, session=SESSION)
        _run_working(home, RUN_PEER, session=PEER)
        crew.list_live(project=PROJECT)

        settles = _drain(
            clock=clock,
            then=lambda: _run_unpromoted(home, RUN_MINE, session=SESSION),
        )
        assert len(settles) == 1, settles
        assert settles[0]["line"] == "", settles[0]["line"]
        assert RUN_MINE not in json.dumps(settles[0]), settles[0]

        # The run the settle row used to name is the one the derivation still
        # holds, and the peer's is work this pane was never asked to watch.
        outstanding = needs_you_module.needs_you_runs(PROJECT, session=SESSION)
        assert [row["run_id"] for row in outstanding] == [RUN_MINE], outstanding
        assert outstanding[0]["kind"] == "unpromoted", outstanding


def test_a_drain_after_the_run_is_promoted_prints_no_line(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet drains with nothing outstanding, and the row is still emitted.

    The row names nothing either way; this is the case that shows the empty
    line is a decision about the line rather than about the fleet.
    """
    monkeypatch.setattr(follow_commands, "FOLLOWER_SETTLE_GRACE_SECONDS", GRACE)
    monkeypatch.setattr(follow_commands, "FOLLOWER_SETTLE_SAMPLE_SECONDS", GRACE)
    clock = _Clock(2000.0)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _run_working(home, RUN_MINE, session=SESSION)
        crew.list_live(project=PROJECT)

        settles = _drain(clock=clock, then=lambda: _promote(RUN_MINE))
        assert len(settles) == 1, settles
        assert settles[0]["line"] == "", settles[0]["line"]
        assert needs_you_module.needs_you_runs(PROJECT, session=SESSION) == []
