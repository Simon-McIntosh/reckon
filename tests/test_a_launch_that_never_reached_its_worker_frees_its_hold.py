"""A launch that never reached its worker frees the hold it took.

The window this closes: a launch takes its reservation, writes its prompt, and
then dies — it raises, returns early, or hangs on storage — before any
supervisor is recorded. Nothing writes the attempt's exit receipt, so the
reservation names the process that reserved it. When that process is a
long-lived door (the admit service, a station) the reservation reads as held for
the door's whole life: nothing retries the run, and a relaunch is refused on a
hold no worker stands behind. One run sat in that state for 1 h 50 min with a
reservation naming the station.

Two facts free it. A launch that ends between taking its reservation and
recording a supervisor files the attempt's exit receipt, naming the cause. And a
reservation naming a live process, with no supervisor recorded and no worker
record for its attempt, stops holding once its age passes
``UNREACHED_LAUNCH_BOUND_SECONDS``. The release is fenced: a launch re-reads its
own reservation immediately before starting a supervisor, and a launch whose
hold was released and retaken abandons, files its receipt, and starts nothing.

Every case works against a temporary crew home and repository, and asserts
afterwards that the operator's own crew home is untouched.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import dispatch_launch as dispatch_launch_module
from reckon.crew import process_liveness
from reckon.crew import runs as runs_module
from reckon.crew.node import CrewError

RUN_ID = "r-unreached-launch"
NODE_ID = "unreached-node"

dispatch_module = importlib.import_module("reckon.crew.dispatch")


@pytest.fixture(autouse=True)
def real_home_untouched() -> None:
    """Every case leaves the operator's own crew home exactly as it found it."""
    real = runs_module.crew_home()
    before = _listing(real)
    yield
    assert _listing(real) == before, "the case wrote into the real crew home"


def _listing(directory: Path) -> list[str] | None:
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one run that has launched before."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setattr(process_liveness, "placement_job_alive", lambda record: None)
    directory = runs_module.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs_module.live_dir().mkdir(parents=True, exist_ok=True)
    runs_module._write_json(runs_module.pointer_path(RUN_ID), _pointer())
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return directory


def _pointer(**extra: object) -> dict:
    return {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "starting",
        "backend": "probe",
        "launch": "cli",
        "attempt": 1,
        "attempt_kind": "resume",
        "session": "session-1",
        "worktree": str(runs_module.run_dir(RUN_ID)),
        "manifest_path": str(runs_module.run_dir(RUN_ID) / "manifest.md"),
        "log_path": str(runs_module.run_dir(RUN_ID) / "stream.jsonl"),
        "stderr_path": str(runs_module.run_dir(RUN_ID) / "stderr.log"),
        "node": {"id": NODE_ID, "plan": "fixture", "section": "guard"},
        **extra,
    }


def _reservation(
    directory: Path,
    attempt: int,
    *,
    pid: int,
    supervisor_pid: int | None = None,
    age: float = 0.0,
) -> Path:
    written = time.time() - age
    payload = {
        "run_id": RUN_ID,
        "attempt": attempt,
        "kind": "dispatch",
        "pid": pid,
        "pid_start_time": runs_module._process_start_time(pid),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(written)),
        "supervisor_pid": supervisor_pid,
        "supervisor_pid_start_time": (
            runs_module._process_start_time(supervisor_pid)
            if supervisor_pid is not None
            else None
        ),
    }
    path = directory / f"attempt-{attempt}.reservation"
    runs_module._write_json(path, payload)
    os.utime(path, (written, written))
    return path


def _live_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)


def _plan(worktree: Path) -> _backends.LaunchPlan:
    return _backends.LaunchPlan(
        backend="alpha",
        dialect="claude",
        argv=[sys.executable, "-c", "pass"],
        cwd=str(worktree),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )


def _launch(directory: Path, worktree: Path) -> int:
    return dispatch_module.supervised_launch(
        _plan(worktree),
        run_directory=directory,
        repo_root=worktree,
        worktree=worktree,
        log_path=directory / "stream.jsonl",
        stderr_path=directory / "stderr.log",
        prompt_path=directory / "prompt.txt",
    )


def _receipts(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.glob("attempt-*-exit.json"))


def _receipt(directory: Path) -> dict:
    names = _receipts(directory)
    assert len(names) == 1, names
    return json.loads((directory / names[0]).read_text(encoding="utf-8"))


# ── a launch that ends before recording a supervisor ────────────────────────


def test_a_launch_that_raises_leaves_its_exit_receipt_and_frees_the_hold(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported shape: reservation and prompt on disk, then nothing."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise OSError("the supervisor could not be started")

    monkeypatch.setattr(dispatch_launch_module, "_start_supervisor", _explode)

    with pytest.raises(OSError, match="could not be started"):
        _launch(home, worktree)

    receipt = _receipt(home)
    assert receipt["run_id"] == RUN_ID
    assert "the supervisor could not be started" in str(receipt.get("cause"))
    assert dispatch_launch_module.held_launch_reservation(home) is None, (
        "the attempt's own exit receipt did not free the hold it took"
    )


# ── a hold that names a live process and no worker ──────────────────────────


def test_a_stale_reservation_naming_a_live_pid_is_not_held_and_a_relaunch_is_admitted(
    home: Path,
) -> None:
    """The station's shape: live pid, no supervisor, no worker, hours old."""
    stale = dispatch_launch_module.UNREACHED_LAUNCH_BOUND_SECONDS + 600
    _reservation(home, 1, pid=os.getpid(), age=stale)

    assert dispatch_launch_module.held_launch_reservation(home) is None, (
        "a reservation that never reached a worker held the run past its bound"
    )
    taken = dispatch_launch_module.reserve_launch_attempt(home, RUN_ID, kind="dispatch")
    assert taken.attempt >= 1
    dispatch_launch_module.release_launch_reservation(taken)


def test_a_hold_within_the_bound_still_holds(home: Path) -> None:
    """A launch slowed by storage is never reclaimed mid-flight."""
    _reservation(home, 1, pid=os.getpid(), age=60.0)
    assert dispatch_launch_module.held_launch_reservation(home) is not None


def test_a_hold_past_the_bound_with_a_recorded_live_supervisor_still_holds(
    home: Path,
) -> None:
    """Age frees only the shape that never reached a supervisor."""
    supervisor = _live_child()
    try:
        stale = dispatch_launch_module.UNREACHED_LAUNCH_BOUND_SECONDS + 600
        _reservation(home, 1, pid=os.getpid(), supervisor_pid=supervisor.pid, age=stale)
        held = dispatch_launch_module.held_launch_reservation(home)
        assert held is not None
        assert held.get("supervisor_pid") == supervisor.pid
    finally:
        _stop(supervisor)


# ── the fence: a released and retaken attempt starts no second supervisor ───


def test_a_launch_whose_attempt_was_retaken_starts_no_supervisor(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release is safe only because the superseded launch abandons."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    started: list[int] = []

    def _capture(*_args: object, **_kwargs: object) -> int:
        started.append(1)
        return 0

    original_spec = dispatch_launch_module._supervisor_spec

    def _spec_after_a_relaunch(*args: object, **kwargs: object):
        # The relaunch reached the run first: it took the attempt for itself and
        # wrote its own reservation over this launch's.
        _reservation(home, 1, pid=os.getpid() + 1)
        return original_spec(*args, **kwargs)

    monkeypatch.setattr(
        dispatch_launch_module, "_supervisor_spec", _spec_after_a_relaunch
    )
    monkeypatch.setattr(dispatch_launch_module, "_start_supervisor", _capture)

    with pytest.raises(CrewError) as refusal:
        _launch(home, worktree)

    assert "abandons without starting a supervisor" in str(refusal.value)
    assert started == [], "a superseded launch started a supervisor anyway"
    receipt = _receipt(home)
    assert receipt["run_id"] == RUN_ID
    assert "abandons" in str(receipt.get("cause"))
