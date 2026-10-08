"""A worker spawn is retried when a protected bind source vanished.

The fence composes a read-only bind for every protected path present at
composition, and a writer that replaces a file by rename leaves the name
missing for an instant — Claude Code replaces ``~/.claude.json`` that way. A
spawn landing in that instant dies inside bubblewrap before the worker starts,
and the run was recorded as a launch failure for a source that was back a
moment later. The spawn is now retried, bounded by the same named constants the
composition's own wait uses, and only the failure bwrap's own refusal proves —
one naming a bind source this launch mounted — is retried.

Every case works against a temporary home, a temporary run directory and
temporary paths, so none reads or writes the launch registries or the
configuration home of the operator who ran it.
"""

from __future__ import annotations

import importlib
import os
import shlex
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _backends, _worker_fence
from reckon.crew import dispatch_launch as dispatch_launch_module
from reckon.crew import runs
from tests.test_fence_protects_the_worktree_pool import (
    Pool,
    _ro_bind_of,
    _without_pool,
)

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RUN_ID = "r-vanished-bind"

# The two phrases bubblewrap answers with when a bind source is gone at mount
# time. Both name the source after the phrase, which is what distinguishes them
# from a refusal over something else.
REFUSALS = ("Can't bind mount", "Can't find source path")

# The cases below exercise the shipped startup window and the shipped attempts
# bound; neither is patched, so the test's verdict is the retry's own decision
# under the constants a dispatch runs with.


@pytest.fixture()
def isolated_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary operator home and a temporary crew home holding one run."""
    home = tmp_path / "home"
    home.mkdir()
    crew_home = tmp_path / "crew-home"
    monkeypatch.setenv("RECKON_HOME", str(crew_home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "sample",
            "phase": "working",
            "backend": "probe",
            "launch": "cli",
            "worktree": str(directory),
            "manifest_path": str(directory / "manifest.md"),
            "log_path": str(directory / "stream.jsonl"),
            "stderr_path": str(directory / "stderr.log"),
            "node": {"id": "node-vanished-bind", "plan": "fixture"},
        },
    )
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return home, directory


def _fenced_argv(home: Path, directory: Path, protected: Path) -> list[str]:
    """Compose a fence the way a dispatch does, over the temporary home."""
    return _backends.fence_argv(
        ["true"],
        writable_directories=[str(directory)],
        home=str(home),
        config={"protected_paths": [str(protected)]},
    )


def _plan(argv: list[str], directory: Path) -> _backends.LaunchPlan:
    return _backends.LaunchPlan(
        backend="probe",
        dialect="",
        argv=argv,
        cwd=str(directory),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _refusal_script(path: Path, refusal: str, source: Path) -> Path:
    """A stand-in for what bwrap prints when a bind source is gone at mount."""
    message = f"bwrap: {refusal} {source} on {source}: No such file or directory"
    return _script(path, f"echo {shlex.quote(message)} >&2\nexit 1\n")


def _reaped(pid: int) -> bool:
    """Whether this launch already collected the child's exit."""
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return bool(reaped)


def _stop(process: subprocess.Popen) -> None:
    """Stop a process this test started and collect it, live or already dead."""
    try:
        os.kill(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - a stop that did not take
        process.kill()
        process.wait(timeout=10)


def _forget(*pids: int) -> None:
    """Drop this test's pids from the launch registries before they are swept."""
    with dispatch_module._LAUNCHED_WORKERS_LOCK:
        for pid in pids:
            dispatch_module._LAUNCHED_WORKERS.discard(pid)
            dispatch_module._LAUNCHED_WORKER_RUNS.pop(pid, None)


def _eventually(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _RecordingSpawn:
    """A ``Popen`` answering with the commands it was given, one per call.

    A spawn refused over a vanished bind source cannot be produced by the real
    ``Popen`` alone — the refusal is the child's own — so the first call runs a
    real short-lived script that reports it, and the second a real live
    process. Every returned object is a real process, so the poll, the reap and
    the pid bookkeeping the launch does are exercised rather than simulated.
    The last command repeats if the launch asks for more tries than commands.
    """

    def __init__(self, commands: list[list[str]]) -> None:
        self.commands = commands
        self.processes: list[subprocess.Popen] = []

    def __call__(self, _argv: list[str], **kwargs: object) -> subprocess.Popen:
        index = min(len(self.processes), len(self.commands) - 1)
        process = subprocess.Popen(
            [str(part) for part in self.commands[index]], **kwargs
        )
        self.processes.append(process)
        return process


def _recording_spawn(
    monkeypatch: pytest.MonkeyPatch, commands: list[list[str]]
) -> _RecordingSpawn:
    """Stand in for ``Popen`` in dispatch's own namespace, and nowhere else."""
    recorder = _RecordingSpawn(commands)
    monkeypatch.setattr(dispatch_launch_module, "subprocess", SimpleNamespace(Popen=recorder))
    return recorder


def _vanish_then_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    directory: Path,
    refusal: str,
) -> tuple[Path, list[str], _RecordingSpawn]:
    """A fenced launch whose first try is refused and whose second starts."""
    protected = home / ".claude.json"
    protected.write_text("{}", encoding="utf-8")
    argv = _fenced_argv(home, directory, protected)
    refused = _refusal_script(
        tmp_path / "refused.sh", refusal, _backends.resolved_destination(protected)
    )
    live = _script(tmp_path / "live.sh", "sleep 30\n")
    spawn = _recording_spawn(monkeypatch, [[str(refused)], [str(live)]])
    return protected, argv, spawn


@pytest.mark.parametrize("refusal", REFUSALS)
def test_a_spawn_refused_over_a_vanished_protected_bind_is_retried(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    home, directory = isolated_run
    protected, argv, spawn = _vanish_then_live(
        tmp_path, monkeypatch, home, directory, refusal
    )
    # Positive control: this launch really does ask the fence to mount the path
    # the refusal names, so the retry is keyed to this launch's own bind.
    sources = dispatch_module._read_only_bind_sources(argv)
    assert str(_backends.resolved_destination(protected)) in sources

    try:
        pid = dispatch_module._spawn_detached_worker(
            _plan(argv, directory),
            log_path=directory / "stream.jsonl",
            stderr_path=directory / "stderr.log",
            prompt_path=directory / "prompt.txt",
        )
        assert len(spawn.processes) == 2, "the refused spawn was not retried"
        assert _reaped(spawn.processes[0].pid), "the refused try was left unreaped"
        assert pid == spawn.processes[-1].pid
        # The refused try is not the launch's outcome: nothing was recorded as
        # a failure for it, and the run's phase is untouched.
        record = runs.read_pointer(RUN_ID)
        assert record.get("launch_failures") in (None, [])
        assert record.get("phase") == "working"
    finally:
        for process in spawn.processes:
            _stop(process)
        _forget(*(process.pid for process in spawn.processes))


def test_a_spawn_that_fails_for_another_reason_is_not_retried(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, directory = isolated_run
    protected = home / ".claude.json"
    protected.write_text("{}", encoding="utf-8")
    argv = _fenced_argv(home, directory, protected)
    doomed = _script(
        tmp_path / "doomed.sh",
        "echo 'exec: cannot run the backend' >&2\nexit 127\n",
    )
    spawn = _recording_spawn(monkeypatch, [[str(doomed)]])
    try:
        pid = dispatch_module._spawn_detached_worker(
            _plan(argv, directory),
            log_path=directory / "stream.jsonl",
            stderr_path=directory / "stderr.log",
            prompt_path=directory / "prompt.txt",
        )
        assert len(spawn.processes) == 1, "a non-bind failure was retried"
        assert pid == spawn.processes[0].pid
        # Positive control for the retry cases: this failure IS the launch
        # failure the launch records, so their untouched run proves the retry
        # absorbed the refused try rather than the recorder ignoring it.
        assert _eventually(
            lambda: (
                runs.read_pointer(RUN_ID).get("phase")
                == dispatch_module.LAUNCH_FAILED_PHASE
            )
        ), "the unrelated spawn failure was not recorded"
    finally:
        for process in spawn.processes:
            _stop(process)
        _forget(*(process.pid for process in spawn.processes))


def test_the_retry_stops_at_the_named_attempt_bound(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, directory = isolated_run
    protected = home / ".claude.json"
    protected.write_text("{}", encoding="utf-8")
    argv = _fenced_argv(home, directory, protected)
    refused = _refusal_script(
        tmp_path / "always-refused.sh",
        REFUSALS[0],
        _backends.resolved_destination(protected),
    )
    spawn = _recording_spawn(monkeypatch, [[str(refused)]])
    try:
        dispatch_module._spawn_detached_worker(
            _plan(argv, directory),
            log_path=directory / "stream.jsonl",
            stderr_path=directory / "stderr.log",
            prompt_path=directory / "prompt.txt",
        )
        # A sweep that never stopped asking would keep spawning; the bound is
        # the shipped attempts constant, so a refusal that outlives it ends as
        # the failure it is.
        assert len(spawn.processes) == _backends.PROTECTED_BIND_WAIT_ATTEMPTS
        assert all(_reaped(process.pid) for process in spawn.processes[:-1]), (
            "a refused try was left unreaped"
        )
        assert _eventually(
            lambda: (
                runs.read_pointer(RUN_ID).get("phase")
                == dispatch_module.LAUNCH_FAILED_PHASE
            )
        ), "the exhausted retry was not recorded as the launch failure"
        failures = runs.read_pointer(RUN_ID).get("launch_failures") or []
        assert len(failures) == 1
    finally:
        for process in spawn.processes:
            _stop(process)
        _forget(*(process.pid for process in spawn.processes))


def test_the_supervisor_seam_retries_the_spawn(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, directory = isolated_run
    _protected, argv, spawn = _vanish_then_live(
        tmp_path, monkeypatch, home, directory, REFUSALS[0]
    )
    spec = {
        "run_id": RUN_ID,
        "prompt_path": str(directory / "prompt.txt"),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "plan": {
            "argv": argv,
            "cwd": str(directory),
            "environment": {},
            "dialect": "",
        },
    }
    try:
        pid = dispatch_module._supervisor_spawn_worker(spec)
        assert len(spawn.processes) == 2, "the supervisor seam did not retry"
        assert pid == spawn.processes[-1].pid
    finally:
        for process in spawn.processes:
            _stop(process)


def test_the_startup_window_tolerates_a_loaded_host() -> None:
    """The shipped window is sized for a loaded host, not for an idle one.

    The window decides whether a launch started, and a tight one turns the
    retry into a race with the host's load: a sandbox that takes longer to
    refuse its mount than the window allows would be recorded as a launch
    failure without the retry ever looking twice. Half a second is the floor
    the window is held to, and the cases above run under this shipped value.
    """
    assert dispatch_module.SANDBOX_STARTUP_WINDOW_SECONDS >= 0.5


def test_the_pool_control_driver_mutation_reaches_the_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = Pool(tmp_path)
    # Positive control: the composed launch really does overlay the pool, so a
    # mutated argv that lacks the overlay is the mutation's doing.
    assert _ro_bind_of(pool.argv(), pool.pool) != -1
    original = _worker_fence.declared_protected_paths
    monkeypatch.setattr(
        _worker_fence,
        "declared_protected_paths",
        _without_pool(pool.home, original),
    )
    assert _ro_bind_of(pool.argv(), pool.pool) == -1
