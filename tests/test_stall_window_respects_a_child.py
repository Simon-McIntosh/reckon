"""A quiet run with a live process under its worker is not stalled.

A worker holding a job — a build, a scheduler reservation, a probe — writes
nothing of its own while the child does the work, so a stream quiet past the
window is that run's normal shape rather than a hang. The window therefore
extends to the run's own time budget while the worker process is alive with a
live process under it, and stays where it was where it is not: a live worker
with nothing running beneath it is the case a stall most often means is hung.

Each case holds one live pointer and a real process, and the two readings
differ in exactly one fact — whether that process has a live process of its own
— with the same declared budget, the same quiet stream and the same window.
Both surfaces that decide a stall are driven here: the ticker, whose rows a
coordinator watches, and the single-event generator a follower arms.

The quiet time is asserted against the fixture's own stream timestamp rather
than against the reading the row took, so a regression inside the descendant
term cannot move both sides of the comparison together.

The declared mutation drops the descendant term from the stall decision in a
scratch copy and leaves the budget term in place, so the sleeping-child case
must go back to stalling at the default window.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import pytest

from reckon.crew import recovery, runs

HOST = socket.gethostname()

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "drop the descendant term from the stall decision in a scratch copy; the "
    "sleeping-child case must fail with a stalled transition"
)

# The project these stub pointers belong to.
PROJECT = "proj"

# The window a quiet run is judged against when its operator names no other.
STALL_WINDOW = "15m"
STALL_SECONDS = 900

# Past the window and short of the run's own budget, so the reading under test
# is decided by the term under test and not by the run's own clock running out.
QUIET_SECONDS = 1013

# The allowance the run declared for its own work, well past both figures, so a
# run that has taken the extension is still inside its own fence.
BUDGET = "1h"
BUDGET_SECONDS = 3600

# A worker that either holds a sleeping child — the shape of a run whose own
# stream is silent because the work is in the process under it — or sleeps
# alone. It sleeps long enough to outlast the case and is ended by this module.
_WORKER_SCRIPT = """\
import subprocess, sys, time

if sys.argv[1] == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    print(child.pid, flush=True)
time.sleep(300)
"""

CHILD_RUN_ID = "r-holding-a-child"
ALONE_RUN_ID = "r-holding-nothing"
SUPERVISED_RUN_ID = "r-behind-a-supervisor"

IN_PROGRESS_MANIFEST = "node: {run_id}\nstatus: in-progress\n"


def _home_fingerprint(home: Path) -> list[tuple[str, int]]:
    """The real config home's own entries, by name and mtime.

    One directory level only: the point is to catch a write that landed in the
    reader's own home, and a recursive walk of a live fleet's home on GPFS is
    the crawl this check must not itself become.
    """
    if not home.is_dir():
        return []
    return sorted((entry.name, entry.stat().st_mtime_ns) for entry in home.iterdir())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run against a temporary config home, and prove the real one untouched.

    The fixture is the receipt for its own isolation: a pointer or a run
    directory written to the real home would both escape the case and collide
    with a live fleet, so the fingerprint is taken before the environment moves
    and the same home is re-read after the case ends.
    """
    real_home = Path(os.path.expanduser("~")) / ".config" / "reckon"
    before = _home_fingerprint(real_home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert _home_fingerprint(real_home) == before


class _PublishedNothing(AssertionError):
    """A watcher reached a tick with nothing to publish, when a row was owed.

    A watcher with nothing to report does not return — it sleeps and reads
    again — so a case that expects a row reads past the moment the row was due
    and finds this instead. It is an assertion rather than a marker because the
    only thing it can mean is that the fleet said something other than what the
    case named.
    """


class _Sleeper:
    """A watcher's sleeper, granting ticks and changing the world between them.

    The tick is the one moment a case may change what the watcher sees, since a
    watcher reads the fleet at the top of each pass and sleeps at the bottom.
    ``actions`` run one per tick, in order, and a tick that had a row to
    publish returns before it ever reaches the sleeper — so the count read
    afterwards is the statement "that many ticks saw the world as it was and
    published nothing".
    """

    def __init__(self, actions: Sequence[Callable[[], None]] = ()) -> None:
        self._actions = list(actions)
        self.spent = 0

    def __call__(self, _seconds: float) -> None:
        if self.spent >= len(self._actions):
            raise _PublishedNothing(
                "the watcher published nothing, so the row this case names "
                "never came"
            )
        action = self._actions[self.spent]
        self.spent += 1
        action()


def _nothing() -> None:
    """A tick that changes nothing, so any row must come from the world as is."""


def _end(pid: int) -> Callable[[], None]:
    """An action that ends one process, in the moment between two readings."""

    def end() -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    return end


def _survivors(pids: Sequence[int], *, grace: float = 5.0) -> list[int]:
    """Which of these pids still runs after a bounded grace.

    Bounded, because a kill is not instantaneous in the process table: a single
    reading taken beside the signal reports a survivor that is already gone,
    and an unbounded wait would hang the suite on a defect instead of reporting
    one.
    """
    deadline = time.monotonic() + grace
    while True:
        live = [pid for pid in pids if runs.process_alive(pid) is True]
        if not live or time.monotonic() >= deadline:
            return live
        time.sleep(0.02)


@contextlib.contextmanager
def _worker(*, with_child: bool) -> Iterator[tuple[int, int | None]]:
    """A live worker process, ended by this module whatever the case does.

    Every process a case starts is named here and killed in the finally — the
    child first, so the worker is never the one left holding it — and the pids
    are read once more after the kill, so a case cannot pass while leaving a
    sleeping process behind it.
    """
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER_SCRIPT, "child" if with_child else "alone"],
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    try:
        if with_child:
            child_pid = int(str(worker.stdout.readline()).strip())
        yield worker.pid, child_pid
    finally:
        started = [pid for pid in (child_pid, worker.pid) if pid is not None]
        for pid in started:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        worker.wait()
        with contextlib.suppress(ValueError, OSError):
            worker.stdout.close()
        stragglers = _survivors(started)
        assert not stragglers, f"processes this case started survived it: {stragglers}"


def _quiet_pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int,
    quiet_seconds: int = QUIET_SECONDS,
    budget: str = BUDGET,
) -> tuple[dict[str, Any], float]:
    """One live pointer whose own stream has been silent past the window.

    Shaped as a pointer on the reading host: the pid is a process this machine
    may ask about, the launcher host is this machine's own name, and the quiet
    time is written into the stream's mtime rather than into any field, which
    is where the reading takes it from. The manifest holds the worker's own
    non-terminal last word beside that silence — a run that reported an outcome
    is a different reading and is not this case.
    """
    stream = tmp_path / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(IN_PROGRESS_MANIFEST.format(run_id=run_id), encoding="utf-8")
    moment = time.time()
    quiet_at = moment - quiet_seconds
    os.utime(stream, (quiet_at, quiet_at))
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": "s22-coord",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": budget},
        "phase": "working",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid),
        "launcher_host": HOST,
        "worktree": str(tmp_path / "tree"),
        "base_sha": "",
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer, moment


def _write_worker_record(run_id: str, pid: int) -> None:
    """The run directory's own record of the worker its supervisor spawned."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.WORKER_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": pid,
                "pid_start_time": recovery._process_start_time(pid),
                "backend": "claude",
                "argv": ["--stub"],
            }
        ),
        encoding="utf-8",
    )


def _fixture_quiet_seconds(pointer: dict[str, Any], moment: float) -> int:
    """The quiet time the fixture itself wrote, from the stream's own mtime.

    Deliberately not the reader the row calls: an expectation taken from that
    reader moves with it, so a regression inside it would leave the row and its
    expectation drifting together and this assertion green.
    """
    return int(moment - os.stat(pointer["log_path"]).st_mtime)


def _row(pointer: dict[str, Any], moment: float) -> dict[str, Any]:
    """The classifier's reading, the one every row and snapshot is built from."""
    return recovery.classify_pointer(
        pointer, now_seconds=moment, stale_after_seconds=STALL_SECONDS
    )


def _snapshot(pointer: dict[str, Any], moment: float) -> dict[str, Any]:
    """The state a ticker compares, reduced from the same reading."""
    return recovery._watch_snapshot(
        pointer, moment=moment, stall_seconds=STALL_SECONDS
    )


def _ticker(sleeper: Callable[[float], None]) -> Iterator[dict[str, Any]]:
    """The surface a coordinator watches: one baseline, then transitions."""
    return recovery.watch_ticker(
        PROJECT, stall_window=STALL_WINDOW, poll_interval=0, sleeper=sleeper
    )


def _watcher(sleeper: Callable[[float], None]) -> Iterator[dict[str, Any]]:
    """The surface a follower arms: the first terminal or stalled run."""
    return recovery.watch_follow(
        PROJECT, stall_window=STALL_WINDOW, poll_interval=0, sleeper=sleeper
    )


def test_a_live_child_holds_the_window_for_the_ticker(tmp_path: Path) -> None:
    """The ticker publishes nothing while the child the worker waits on lives.

    The kill is delivered on the third tick, so the run is quiet with a live
    child across three readings that produce no row, and the reading after the
    child ends is the first that may call the run stalled. A window that did
    not respect the child would have published the stall as the baseline, so
    the baseline is asserted first and on its own: it is the row at which a
    reading that ignored the child announces itself.
    """
    with _worker(with_child=True) as (worker_pid, child_pid):
        assert child_pid is not None
        pointer, moment = _quiet_pointer(tmp_path, CHILD_RUN_ID, pid=worker_pid)
        quiet = _fixture_quiet_seconds(pointer, moment)
        assert quiet > STALL_SECONDS, quiet
        # Read while the child lives, before the watcher is driven: this case is
        # about what the reading says of a run that is quiet on purpose.
        row = _row(pointer, moment)
        snapshot = _snapshot(pointer, moment)

        sleeper = _Sleeper([_nothing, _nothing, _end(child_pid)])
        stream = _ticker(sleeper)
        try:
            baseline = next(stream)
            assert baseline["to_state"] == "working", baseline
            transition = next(stream)
        finally:
            stream.close()

        assert sleeper.spent == 3, sleeper.spent
        assert transition["from_state"] == "working", transition
        assert transition["to_state"] == "stalled", transition
        assert transition["process_alive"] is True, transition
        assert transition["detail"].startswith("alive, quiet "), transition["detail"]
        assert row["budget_seconds"] == BUDGET_SECONDS, row["budget_seconds"]
        assert row["process_alive"] is True, row["detail"]
        assert row["process_descendant_alive"] is True, row["detail"]
        assert snapshot["state"] == "working", snapshot["detail"]


def test_a_live_child_holds_the_window_for_the_watch_generator(
    tmp_path: Path,
) -> None:
    """The generator a follower arms defers the run the same way.

    A follower has no baseline: its first event is the news, so a window that
    ignored the child would wake its reader immediately with the wrong one.
    """
    with _worker(with_child=True) as (worker_pid, child_pid):
        assert child_pid is not None
        pointer, moment = _quiet_pointer(tmp_path, CHILD_RUN_ID, pid=worker_pid)
        quiet = _fixture_quiet_seconds(pointer, moment)
        assert quiet > STALL_SECONDS, quiet

        sleeper = _Sleeper([_nothing, _nothing, _end(child_pid)])
        stream = _watcher(sleeper)
        try:
            event = next(stream)
        finally:
            stream.close()

        assert sleeper.spent == 3, (
            f"the follower was woke after {sleeper.spent} tick(s) with the "
            "child alive"
        )
        assert event["event"] == "stalled", event
        assert event["run_id"] == CHILD_RUN_ID, event
        assert event["stalled_for_seconds"] > STALL_SECONDS, event
        assert event["process_alive"] is True, event


def test_a_live_worker_with_nothing_under_it_stalls_at_the_window(
    tmp_path: Path,
) -> None:
    """The narrowing kept on purpose: a live worker under nothing reads stalled.

    Same budget, same quiet stream and same window as the case above, and the
    only difference is that this worker holds no process — which is the shape a
    stall most often means is hung.
    """
    with _worker(with_child=False) as (worker_pid, child_pid):
        assert child_pid is None
        pointer, moment = _quiet_pointer(tmp_path, ALONE_RUN_ID, pid=worker_pid)
        quiet = _fixture_quiet_seconds(pointer, moment)
        assert quiet > STALL_SECONDS, quiet

        row = _row(pointer, moment)
        assert row["budget_seconds"] == BUDGET_SECONDS, row["budget_seconds"]
        assert row["process_alive"] is True, row["detail"]
        assert row["process_descendant_alive"] is False, row["detail"]
        snapshot = _snapshot(pointer, moment)
        assert snapshot["state"] == "stalled", snapshot["detail"]
        assert snapshot["detail"].startswith("alive, quiet "), snapshot["detail"]

        ticker = _ticker(_Sleeper())
        try:
            baseline = next(ticker)
        finally:
            ticker.close()
        assert baseline["to_state"] == "stalled", baseline

        watcher = _watcher(_Sleeper())
        try:
            event = next(watcher)
        finally:
            watcher.close()
        assert event["event"] == "stalled", event
        assert event["stalled_for_seconds"] > STALL_SECONDS, event


def test_the_supervised_worker_is_the_process_asked_about_its_child(
    tmp_path: Path,
) -> None:
    """The pid asked is the worker's, not the supervisor's, under a live child.

    A supervised launch writes the supervisor's pid on the pointer while the
    work happens in the worker it spawned, and the supervisor holds that worker
    as a child.

    The supervisor in this case is a live process with no child of its own, so
    a reading that asked the pointer's pid would read no descendant and stall
    the run — which is what this case would report instead of a live child.
    """
    with _worker(with_child=False) as (supervisor_pid, _supervisor_child):
        with _worker(with_child=True) as (worker_pid, child_pid):
            assert child_pid is not None
            pointer, moment = _quiet_pointer(
                tmp_path, SUPERVISED_RUN_ID, pid=supervisor_pid
            )
            _write_worker_record(SUPERVISED_RUN_ID, worker_pid)
            quiet = _fixture_quiet_seconds(pointer, moment)
            assert quiet > STALL_SECONDS, quiet

            row = _row(pointer, moment)
            assert row["process_alive"] is True, row["detail"]
            assert row["process_descendant_alive"] is True, row["detail"]
            snapshot = _snapshot(pointer, moment)
            assert snapshot["state"] == "working", snapshot["detail"]