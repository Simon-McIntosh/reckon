"""An attempt's exit record waits for the worker its launcher started.

Some lanes launch a worker through an intermediate: the process the supervisor
starts exits within seconds while the worker it started runs on. Written at the
launcher's exit, the attempt's record would name a dead intermediate and lose
the live worker -- measured on a fleet, an exit recorded three seconds after
launch while the worker went on committing for six minutes.

The supervisor is a child subreaper, so the orphaned worker is reparented to
it, and the exit record is written when that worker ends and carries the
worker's own exit. The declared mutation writes the record as soon as the
immediate child exits, exactly as before the wait: a record then appears while
the worker is alive and the case fails.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import reckon.crew.dispatch_launch as dispatch_launch_module
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The launcher starts the worker the run is about and exits at once, with its
# own distinct status so a record naming the launcher is separable from one
# naming the worker. The worker's own sleep bounds it, so a case that fails
# before ending it cannot leave it behind for longer than this; the case always
# ends the worker by its pid as well.
LAUNCHER = (
    "import os, subprocess, sys\n"
    "from pathlib import Path\n"
    "worker = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
    ")\n"
    "Path(os.environ['RECKON_TEST_WORKER_PID_FILE']).write_text(str(worker.pid))\n"
    "sys.exit(3)\n"
)

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's facts.
DECLARED_MUTATION = (
    "With the supervisor writing the exit record as soon as its immediate child "
    "exits, tests/test_attempt_exit_waits_for_its_worker.py fails because an "
    "exit record appears while the worker is alive."
)

NEGATIVE_CONTROL = os.environ.get("RECKON_ATTEMPT_EXIT_NEGATIVE_CONTROL", "").strip()

# How long the case holds while the worker lives, before asserting no record is
# present. Longer than the supervisor's poll, so a record written at the
# launcher's exit has had every chance to appear first.
HOLD_SECONDS = 5.0
# How long the case allows for the record to appear once the worker has ended.
RECORD_TIMEOUT_SECONDS = 60.0


def _control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation that makes the case go red."""
    if NEGATIVE_CONTROL in {"write-at-launcher-exit", "1"}:
        # Write the exit record as soon as the immediate child exits, exactly
        # as before the worker wait: the record appears while the worker lives.
        monkeypatch.setattr(
            dispatch_launch_module,
            "_reap_the_launched_worker",
            lambda pid, status, **_kwargs: (pid, status),
        )


def _running(pid: int | None) -> bool:
    """Whether a pid is a live (non-zombie) process."""
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    # The command name may hold spaces and parentheses; the state is the field
    # after the final ')'.
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def _wait_until(predicate: Callable[[], Any], *, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{detail} not met within {timeout:g}s")


def _kill(pid: int | None) -> None:
    if not pid:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _launcher_pid(run_id: str) -> int | None:
    """The pid the supervisor spawned, from the worker record it wrote."""
    path = runs.run_dir(run_id) / dispatch_module.WORKER_RECORD_NAME
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _stub_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A live pointer and the spec that supervises a launcher for it."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id = "r-launcher-exits-ahead-of-its-worker"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = tmp_path / "manifest.md"
    stream = directory / "stream.jsonl"
    prompt = directory / "prompt.txt"
    prompt.write_text("stub prompt\n", encoding="utf-8")
    worker_pid_file = tmp_path / "worker.pid"

    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": "launcher-exit-fixture",
        "repo": str(worktree),
        "worktree": str(worktree),
        "backend": "alpha",
        "launch": "cli",
        "dialect": "claude",
        "phase": "starting",
        "pid": None,
        "session": "coordinator-fixture",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-10-02T00:00:00Z",
        "created_at": "2026-10-02T00:00:00Z",
        "node": {
            "id": "stub-node",
            "plan": "fixture",
            "time_budget": "20m",
            "manifest_path": str(manifest),
        },
    }
    runs._write_json(directory / dispatch_module.ATTEMPT_RECORD_NAME, {"attempt": 1})
    runs._write_json(runs.pointer_path(run_id), pointer)

    spec = {
        "run_id": run_id,
        "run_directory": str(directory),
        "repo": str(worktree),
        "worktree": str(worktree),
        "fenced": False,
        "plan": {
            "argv": [sys.executable, "-c", LAUNCHER],
            "cwd": str(worktree),
            "environment": {"RECKON_TEST_WORKER_PID_FILE": str(worker_pid_file)},
            "dialect": "claude",
            "backend": "alpha",
        },
        "prompt_path": str(prompt),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-10-02T00:00:00Z",
        "environment": {},
    }
    spec_path = directory / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return {
        "run_id": run_id,
        "directory": directory,
        "manifest": manifest,
        "worker_pid_file": worker_pid_file,
        "spec_path": spec_path,
    }


def _drive(spec_path: Path, driver: Callable[[], None]) -> None:
    """Run the supervisor in the main thread while ``driver`` observes.

    ``_run_supervisor`` installs signal handlers, which only the main thread
    may do, so the supervisor runs here and the observations run beside it.
    """
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    thread = threading.Thread(target=driver, name="attempt-exit-driver")
    thread.start()
    try:
        dispatch_module._run_supervisor(spec_path)
    finally:
        thread.join(timeout=120)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def test_the_exit_record_waits_for_the_worker_behind_a_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dispatch_module.TERMINAL_MANIFEST_GRACE_ENV, "5")
    _control(monkeypatch)
    fixture = _stub_run(tmp_path, monkeypatch)
    run_id = fixture["run_id"]
    directory = fixture["directory"]
    exit_path = directory / dispatch_module.EXIT_RECORD_NAME
    attempt_exit_path = dispatch_module._attempt_artifact_path(
        directory, dispatch_module.EXIT_RECORD_NAME, 1
    )

    failures: list[BaseException] = []

    def observe() -> None:
        worker_pid: int | None = None
        try:
            _wait_until(
                lambda: _launcher_pid(run_id) is not None,
                timeout=30,
                detail="the supervisor's worker record",
            )
            launcher_pid = _launcher_pid(run_id)
            _wait_until(
                lambda: not _running(launcher_pid),
                timeout=30,
                detail="the launcher's exit",
            )
            _wait_until(
                fixture["worker_pid_file"].exists,
                timeout=30,
                detail="the launched worker's pid",
            )
            worker_pid = int(fixture["worker_pid_file"].read_text(encoding="utf-8"))
            # The launcher is gone and the worker lives on. A record written at
            # the launcher's exit is present by now; the fixed supervisor is
            # still waiting for the worker and has written none.
            time.sleep(HOLD_SECONDS)
            assert _running(worker_pid), "the worker ended before the case ended it"
            assert not exit_path.exists(), (
                "an exit record appeared while the worker lived"
            )
            assert not attempt_exit_path.exists(), (
                "an attempt exit record appeared while the worker lived"
            )
            os.kill(worker_pid, signal.SIGKILL)
            _wait_until(
                exit_path.exists,
                timeout=RECORD_TIMEOUT_SECONDS,
                detail="the exit record after the worker's end",
            )
            record = json.loads(exit_path.read_text(encoding="utf-8"))
            assert record.get("worker_pid") == worker_pid, record
            assert record.get("signal_name") == "SIGKILL", record
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            failures.append(exc)
        finally:
            _kill(worker_pid)

    _drive(fixture["spec_path"], observe)
    if failures:
        raise failures[0]
