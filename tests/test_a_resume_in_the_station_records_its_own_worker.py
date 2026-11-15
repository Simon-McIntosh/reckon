"""A resume started inside the station records its own worker, never the station.

The reported shape: a recovery-sweep resume launched from inside the station
wrote an attempt reservation naming the station's own process as the attempt's
process. The station outlives every run it launches, so the hold read as in
flight for the station's whole life and nothing could declare the attempt
ended. Worse, the record named a pid that ``crew stop`` and ``crew discard`` act
on: signalling it ends the process holding every session's claims.

Three readings are held here. A launch made inside a long-lived process records
the supervisor it started, not the process that asked for it. A reservation
naming the reading host's station names no process of the run, so it neither
holds the attempt nor points a signal at the station. And neither a stop nor a
discard signals a pid that belongs to the station, whatever the record says.

Every case works against a temporary crew home and a temporary node runtime
root, and asserts afterwards that the operator's own crew home is untouched.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import dispatch_launch as launch
from reckon.crew import process_liveness, runs
from reckon.crew.node import CrewError

RUN_ID = "r-station-pid"
DISCARD_RUN_ID = "r-station-pid-discard"
NODE_ID = "station-pid-node"

dispatch_module = importlib.import_module("reckon.crew.dispatch")
crew_module = importlib.import_module("reckon.crew")


@pytest.fixture(autouse=True)
def real_home_untouched():
    """Every case leaves the operator's own crew home exactly as it found it."""
    real = runs.crew_home()
    before = _listing(real)
    yield
    assert _listing(real) == before, "the case wrote into the real crew home"


def _listing(directory: Path) -> list[str] | None:
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture()
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and node runtime root, with one run directory."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return directory


def _pointer(run_id: str, **extra: object) -> dict:
    directory = runs.run_dir(run_id)
    return {
        "run_id": run_id,
        "project": "sample",
        "phase": "starting",
        "backend": "probe",
        "launch": "cli",
        "attempt": 1,
        "attempt_kind": "resume",
        "session": "session-1",
        "worktree": str(directory),
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "node": {"id": NODE_ID, "plan": "fixture", "section": "guard"},
        **extra,
    }


def _write_pointer(run_id: str, **extra: object) -> None:
    runs._write_json(runs.pointer_path(run_id), _pointer(run_id, **extra))


def _write_station_lease(pid: int) -> None:
    """Write this node's station lease naming ``pid``, as the station does."""
    path = runs.station_lease_path()
    assert path is not None, "the case set no node runtime root"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "host": socket.gethostname(),
                "pid": pid,
                "job": "",
                "token": "station-pid-case",
                "pid_namespace": os.readlink("/proc/self/ns/pid"),
                "pid_start": runs._process_start_time(pid),
            }
        ),
        encoding="utf-8",
    )


def _reservation(
    directory: Path,
    attempt: int,
    *,
    pid: int | None,
    supervisor_pid: int | None = None,
    age: float = 0.0,
) -> Path:
    written = time.time() - age
    path = directory / f"attempt-{attempt}.reservation"
    runs._write_json(
        path,
        {
            "run_id": directory.name,
            "attempt": attempt,
            "kind": "resume",
            "pid": pid,
            "pid_start_time": (
                runs._process_start_time(pid) if pid is not None else None
            ),
            "launcher_host": socket.gethostname(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(written)),
            "supervisor_pid": supervisor_pid,
            "supervisor_pid_start_time": (
                runs._process_start_time(supervisor_pid)
                if supervisor_pid is not None
                else None
            ),
        },
    )
    os.utime(path, (written, written))
    return path


def _live_child() -> subprocess.Popen:
    """A live process leading its own group, the shape a station runs in."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def _plan() -> _backends.LaunchPlan:
    return _backends.LaunchPlan(
        backend="alpha",
        dialect="claude",
        argv=[sys.executable, "-c", "pass"],
        cwd=str(Path.cwd()),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )


def _launch(directory: Path) -> int:
    return dispatch_module.supervised_launch(
        _plan(),
        run_directory=directory,
        repo_root=directory,
        worktree=directory,
        log_path=directory / "stream.jsonl",
        stderr_path=directory / "stderr.log",
        prompt_path=directory / "prompt.txt",
    )


def _only_reservation(directory: Path) -> dict:
    found = launch.latest_reservation(directory)
    assert found is not None, "the launch took no reservation"
    return json.loads(found[1].read_text(encoding="utf-8"))


def _sender_pids(directory: Path) -> list[int]:
    """The pids the run's own sender record names, in the order it was written."""
    pids: list[int] = []
    try:
        text = (directory / "senders.jsonl").read_text(encoding="utf-8")
    except OSError:
        return pids
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        pid = payload.get("target_pid")
        if isinstance(pid, int):
            pids.append(pid)
    return pids


# ── the writer: a launch inside the station names the supervisor it started ──


def test_a_launch_inside_the_station_records_the_supervisor_it_started(
    node: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt's process is the supervisor, and the door is recorded beside it."""
    _write_pointer(RUN_ID)
    # This process holds the node's station lease, so it is the station: the
    # long-lived door the reported reservation named.
    _write_station_lease(os.getpid())
    supervisor = _live_child()
    monkeypatch.setattr(
        launch, "_start_supervisor", lambda *_args, **_kwargs: supervisor.pid
    )
    try:
        started = _launch(node)
    finally:
        _stop(supervisor)

    assert started == supervisor.pid
    payload = _only_reservation(node)
    assert payload["pid"] == supervisor.pid, (
        "the reservation names the launcher rather than the supervisor it started"
    )
    assert payload["pid"] != os.getpid()
    assert payload["supervisor_pid"] == supervisor.pid
    assert payload["launcher_pid"] == os.getpid(), (
        "the door that took the reservation is not recorded beside the attempt"
    )


# ── the reader: a reservation naming the station names no process of the run ──


def test_a_reservation_naming_the_station_is_not_read_as_the_runs_process(
    node: Path,
) -> None:
    """The reported hold: a worker that never spawned, the station named instead."""
    station = _live_child()
    try:
        _write_station_lease(station.pid)
        _reservation(node, 1, pid=station.pid, supervisor_pid=station.pid)

        assert launch.held_launch_reservation(node) is None, (
            "a reservation naming the station held the attempt"
        )
        taken = launch.reserve_launch_attempt(node, RUN_ID, kind="resume")
        try:
            assert taken.attempt >= 1
        finally:
            launch.release_launch_reservation(taken)
        assert station.poll() is None
    finally:
        _stop(station)


# ── the stop and the discard: neither signals the station ────────────────────


def test_a_stop_and_a_discard_never_signal_the_pid_that_belongs_to_the_station(
    node: Path,
) -> None:
    """A record naming the station is read as naming no process of the run."""
    station = _live_child()
    discard_directory = runs.run_dir(DISCARD_RUN_ID)
    discard_directory.mkdir(parents=True)
    try:
        _write_station_lease(station.pid)
        for run_id in (RUN_ID, DISCARD_RUN_ID):
            _write_pointer(
                run_id,
                phase="working",
                pid=station.pid,
                pid_start_time=runs._process_start_time(station.pid),
                launcher_host=socket.gethostname(),
            )

        # What every such reader takes: a record naming the station names no
        # process of the run.
        assert runs.read_pointer(RUN_ID).get("pid") is None

        with pytest.raises(CrewError) as refusal:
            crew_module.terminate(RUN_ID)
        assert "has no process to stop" in str(refusal.value)
        assert station.poll() is None, "the stop signalled the station"
        assert station.pid not in _sender_pids(node), (
            "the stop aimed a signal at the station"
        )

        discarded = crew_module.discard(DISCARD_RUN_ID)
        assert discarded["pointer_removed"] is True
        assert station.poll() is None, "the discard signalled the station"
    finally:
        _stop(station)
