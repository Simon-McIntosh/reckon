"""The follower's settle row says whether the session's own fleet is drained.

A pane goes dark exactly when a coordinator looks up from it, and the row it
leaves behind is the last thing that pane says. That row already names when the
session's work stopped; these cases hold it to saying what the reader must do
about it -- the session's fleet is drained, or the count of runs it still owes
the coordinator -- counted by the same derivation the standing needs-you line
reads, so the closing row and the line it closed cannot disagree. The row leads
with when it stopped -- ``no live work since`` and the HH:MM stamp -- and names
a session only when the pane watches more than its own fleet.

Two facts decide the shape of the cases below.

The settle is hinged on the pane having watched its fleet live, because a
dispatch arms a follower before it publishes the run's pointer: a follower armed
into an already quiet session keeps following. So every drain here is reached
from a run the pane saw working, and the second drain is reached the same way.

The tail counts the owning session's runs alone. A peer session's working run in
the same fixture is work this pane was never asked to watch, so it neither holds
the pane open nor appears in the row.
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
# test is the one a real follower runs with.
GRACE = follow_commands.FOLLOWER_SETTLE_GRACE_SECONDS
# The wait pass's step, real time only: the cases' own clock never advances with
# it, so an empty window lasts exactly as long as the case says.
POLL = 0.005
# How long a case waits for the pane to drain. The pane is a thread on a machine
# carrying a whole test fleet when this runs, and the closing row reads the
# session's outstanding runs off disk before it can be printed, so a bound tight
# enough to trip on a loaded worker measures the worker and not the pane.
DRAIN_BOUND = 20.0


class _Clock:
    """A monotonic stand-in the case advances by hand."""

    def __init__(self, start: float) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Wall:
    """A wall clock the case sets, so the row's stamp is exact.

    Everything but ``time`` is the real module's, so the row's own
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

    def __init__(self, clock: _Clock, *, remind: float | None = None):
        self.clock = clock
        self.remind = remind
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
            remind=self.remind,
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


def _drain(
    *,
    clock: _Clock,
    wall: _Wall,
    sighting_wall: float,
    then,
    remind: float | None = None,
) -> list[dict]:
    """Arm one follower over the run live, apply ``then``, and let it drain.

    The run is already live when this is called, so the pane has work to watch:
    that sighting is the hinge the settle is judged on. The wall clock is set to
    the moment of the sighting and moved on afterwards to a time whose HH:MM
    differs, so a row printing the later clock could not be naming the sighting.
    """
    wall.value = sighting_wall
    arming = _Arming(clock, remind=remind).start()
    try:
        _wait_until(
            lambda: follow_checkpoint.exists(PROJECT, SESSION),
            timeout=5.0,
            description="the arming to attach and record its place",
        )
        arming.clock.advance(5.0)
        time.sleep(0.5)
        then()
        wall.value = sighting_wall + 2 * 3600
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


def _prefix(sighting_wall: float) -> str:
    stamp = time.strftime("%H:%M", time.localtime(sighting_wall))
    return f"no live work since {stamp}"


def test_a_drain_names_the_run_that_needs_you(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drained pane says what its session still owes, then says it is drained.

    The owning session's last working run ends unpromoted, so the row's tail
    names one run needing the coordinator. A peer session's working run sits in
    the same fixture, unobserved, and neither holds the pane open nor appears in
    the row. The run is then live again and is promoted, and the next drain says
    the fleet is drained instead.
    """
    sighting = time.time() - 3 * 3600
    wall = _Wall(sighting)
    clock = _Clock(2000.0)
    monkeypatch.setattr(follow_commands, "time", wall)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _run_working(home, RUN_MINE, session=SESSION)
        _run_working(home, RUN_PEER, session=PEER)
        crew.list_live(project=PROJECT)

        settles = _drain(
            clock=clock,
            wall=wall,
            sighting_wall=sighting,
            then=lambda: _run_unpromoted(home, RUN_MINE, session=SESSION),
        )
        assert len(settles) == 1, settles
        line = settles[0]["line"]
        prefix = _prefix(sighting)
        assert line.startswith(prefix), line
        tail = line[len(prefix) :]
        # The arming carried no cadence, so no standing line was ever shown:
        # the set the pane's last needs-you line printed is nothing, and every
        # run still outstanding is named by this closing row.
        assert tail == f" · needs you: {RUN_MINE}", (
            f"the drained pane does not name the run still owed: {line!r}"
        )
        # The peer's working run is work this pane was never asked to watch.
        assert PEER not in line and RUN_PEER not in line, line
        # The row and the standing line read one derivation over one fixture.
        outstanding = needs_you_module.needs_you_runs(PROJECT, session=SESSION)
        assert [row["run_id"] for row in outstanding] == [RUN_MINE], outstanding
        assert outstanding[0]["kind"] == "unpromoted", outstanding

        _run_working(home, RUN_MINE, session=SESSION)
        settles = _drain(
            clock=clock,
            wall=wall,
            sighting_wall=sighting + 2 * 3600,
            then=lambda: _promote(RUN_MINE),
        )
        assert len(settles) == 1, settles
        line = settles[0]["line"]
        prefix = _prefix(sighting + 2 * 3600)
        assert line.startswith(prefix), line
        assert "fleet drained" in line[len(prefix) :], line
        assert needs_you_module.needs_you_runs(PROJECT, session=SESSION) == []


def test_a_drain_keeps_silent_about_a_set_the_standing_line_named(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set the standing line already printed is not restated by the closing row.

    Restating a standing fact is what made the pane noise, and the closing row
    is the pane's last one: a reader who was already told which runs are owed
    learns nothing from hearing it again. This arming carries a cadence, so the
    standing line fires while the pane lives and the closing row has nothing
    left to add.
    """
    sighting = time.time() - 3 * 3600
    wall = _Wall(sighting)
    clock = _Clock(2000.0)
    monkeypatch.setattr(follow_commands, "time", wall)
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        _run_working(home, RUN_MINE, session=SESSION)
        crew.list_live(project=PROJECT)

        settles = _drain(
            clock=clock,
            wall=wall,
            sighting_wall=sighting,
            remind=1.0,
            then=lambda: _run_unpromoted(home, RUN_MINE, session=SESSION),
        )
        assert len(settles) == 1, settles
        line = settles[0]["line"]
        assert line == _prefix(sighting), (
            f"the closing row restated a set the standing line already named: {line!r}"
        )
