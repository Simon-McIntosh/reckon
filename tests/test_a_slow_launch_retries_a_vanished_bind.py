"""A launch that meets the vanished bind refusal slowly is still retried.

The fence composes a read-only bind for every protected path present at
composition, and a writer that replaces a file by rename leaves the name
missing for an instant. A launch that meets that instant is refused by
bubblewrap before the worker starts, and is retried when it does rather than
recorded as a launch failure.

The retry has to reach a launch whose refusal arrives *after* the child has been
running for a while, because on a placed launch the child is the local scheduler
client: it stays alive while the worker's step starts, so the four seconds the
recorded launch took from launch to exit is the normal shape of this failure,
not an outlier. A watch that keys on the child's own early exit misses it and
the launch is recorded as failed. These cases drive the shipped watch over a
control watch that ends on the launch's own stream record, over a refusal that
lands four seconds in, over a refusal over something else at the same delay, and
over healthy launches that must not be delayed by it.

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

from reckon import _backends
from reckon.crew import runs

# The module that owns the spawn seam: ``_spawn_detached_worker`` resolves
# ``subprocess`` and the launch registries in its own namespace, so a stand-in
# for ``Popen`` has to be installed here and nowhere else.
dispatch_module = importlib.import_module("reckon.crew.dispatch_launch")

RUN_ID = "r-slow-launch"

# The delay between launch and the refusal, taken from the recorded launch that
# motivated this case: the step start precedes the sandbox's own refusal by
# seconds, so a watch that only covers the sandbox's own startup never sees it.
RECORDED_LAUNCH_DELAY_SECONDS = 4.0

# The two phrases bubblewrap answers with when a bind source is gone at mount
# time. Both name the source after the phrase, which is what distinguishes them
# from a refusal over something else.
REFUSALS = ("Can't bind mount", "Can't find source path")

# The shipped watch's own window, read once so a case can say what it measured
# against without restating the constant.
WINDOW = dispatch_module.SANDBOX_STARTUP_WINDOW_SECONDS


@pytest.fixture()
def isolated_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary operator home and a temporary crew home holding one run."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
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
            "node": {"id": "node-slow-launch", "plan": "fixture"},
        },
    )
    (directory / "prompt.txt").write_text("do the work\n", encoding="utf-8")
    return home, directory


def _script(path: Path, body: str, *, delay: float = 0.0) -> Path:
    """A shell script that runs ``body``, optionally after ``delay`` seconds."""
    prefix = f"sleep {delay}\n" if delay else ""
    path.write_text(f"#!/bin/sh\n{prefix}{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _refusal_script(path: Path, refusal: str, source: str, *, delay: float) -> Path:
    """A stand-in for a placed sandbox that refuses ``delay`` seconds in."""
    text = f"bwrap: {refusal} {source} on {source}: No such file or directory"
    body = f"echo {shlex.quote(text)} >&2\nexit 1\n"
    return _script(path, body, delay=delay)


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


def _launch(argv: list[str], directory: Path) -> int:
    """Run the shipped detached launch over one fake sandbox argv."""
    return dispatch_module._spawn_detached_worker(
        _plan(argv, directory),
        log_path=directory / "stream.jsonl",
        stderr_path=directory / "stderr.log",
        prompt_path=directory / "prompt.txt",
    )


class _RecordingSpawn:
    """A ``Popen`` answering with the commands it was given, one per call.

    A spawn whose child refuses slowly cannot be produced by the real ``Popen``
    alone — the refusal is the child's own and its timing is the launch's — so
    each call runs a real short-lived or long-lived script. Every returned
    object is a real process, so the poll, the reap and the pid bookkeeping the
    launch does are exercised rather than simulated. The last command repeats
    if the launch asks for more tries than commands.
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
    monkeypatch.setattr(dispatch_module, "subprocess", SimpleNamespace(Popen=recorder))
    return recorder


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


def _cleanup(*processes: subprocess.Popen) -> None:
    """Detach this test's pids from the registry, then end them.

    The registry is emptied first so the launch's own reaper cannot write the
    run's pointer while pytest removes this case's temporary tree: only a pid
    still registered is ever written about.
    """
    _forget(*(process.pid for process in processes))
    for process in processes:
        _stop(process)


def _eventually(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.mark.parametrize("refusal", REFUSALS)
def test_a_launch_refused_after_the_step_starts_is_retried(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    """The recorded failure's own delay is inside the watch, so it retries."""
    home, directory = isolated_run
    protected = home / ".claude.json"
    argv = _fenced_argv(home, directory, protected)
    source = str(_backends.resolved_destination(protected))
    refused = _refusal_script(
        tmp_path / "refused.sh",
        refusal,
        source,
        delay=RECORDED_LAUNCH_DELAY_SECONDS,
    )
    # The second attempt reaches its turn, so the launch returns as soon as the
    # worker's stream begins, and the elapsed time below measures the first
    # attempt's watch rather than the second's own window.
    live = _script(
        tmp_path / "live.sh",
        'echo \'{"type":"thread.started"}\'\nsleep 30\n',
    )
    spawn = _recording_spawn(monkeypatch, [[str(refused)], [str(live)]])

    # Positive control: this launch really asks the fence to mount the path the
    # refusal names, so the retry is keyed to this launch's own bind.
    assert source in dispatch_module._read_only_bind_sources(argv)

    try:
        started = time.monotonic()
        pid = _launch(argv, directory)
        assert len(spawn.processes) == 2, (
            "a refusal that landed after the step started was not retried"
        )
        assert _reaped(spawn.processes[0].pid), "the refused try was left unreaped"
        assert pid == spawn.processes[-1].pid
        # The refused try is not the launch's outcome: nothing was recorded as a
        # failure for it, and the run's phase is untouched.
        record = runs.read_pointer(RUN_ID)
        assert record.get("launch_failures") in (None, [])
        assert record.get("phase") == "working"
        # The retry ends on the refusal, not on the window: this case's watch is
        # the four seconds the refusal takes, not the whole bound.
        assert time.monotonic() - started < WINDOW, (
            "the retry waited the window out instead of ending on the refusal"
        )
    finally:
        _cleanup(*spawn.processes)


def test_a_launch_refused_over_something_else_after_the_same_delay_is_not_retried(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the bind-source refusal is retried, not every slow launch failure."""
    home, directory = isolated_run
    protected = home / ".claude.json"
    argv = _fenced_argv(home, directory, protected)
    doomed = _script(
        tmp_path / "doomed.sh",
        "echo 'exec: cannot run the backend' >&2\nexit 1\n",
        delay=RECORDED_LAUNCH_DELAY_SECONDS,
    )
    spawn = _recording_spawn(monkeypatch, [[str(doomed)]])

    try:
        pid = _launch(argv, directory)
        assert len(spawn.processes) == 1, "a non-bind refusal was retried"
        assert pid == spawn.processes[0].pid
        # This failure reaches the launch after the same four seconds the bind
        # refusal does, so the delay alone reproduces the recorded failure: only
        # the refusal's text separates the retried case from this one.
        assert _eventually(
            lambda: (
                runs.read_pointer(RUN_ID).get("phase")
                == dispatch_module.LAUNCH_FAILED_PHASE
            )
        ), "the delayed non-bind launch failure was not recorded"
    finally:
        _cleanup(*spawn.processes)


def test_a_healthy_launch_returns_when_its_window_passes(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent child that stays alive ends the watch, not the launch.

    The launch returns once its window passes with no refusal, leaving the child
    running: an implementation that keys on the child's exit would wait for a
    child that never exits.
    """
    home, directory = isolated_run
    protected = home / ".claude.json"
    argv = _fenced_argv(home, directory, protected)
    healthy = _script(tmp_path / "healthy.sh", "sleep 60\n")
    spawn = _recording_spawn(monkeypatch, [[str(healthy)]])

    try:
        started = time.monotonic()
        pid = _launch(argv, directory)
        elapsed = time.monotonic() - started
        assert len(spawn.processes) == 1, "a healthy launch was spawned twice"
        assert pid == spawn.processes[0].pid
        assert spawn.processes[0].poll() is None, (
            "the launch waited for a child that outlives the window"
        )
        assert elapsed >= WINDOW, (
            "the launch returned before its window passed, so it never "
            "exercised the window's bound; this case would then measure nothing"
        )
        record = runs.read_pointer(RUN_ID)
        assert record.get("launch_failures") in (None, [])
        assert record.get("phase") == "working"
    finally:
        _cleanup(*spawn.processes)


def test_a_running_worker_quoting_the_refusal_is_not_a_dead_try(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live worker's own words must not be read as a refused launch.

    The refusal is read from the launch's own stderr, and the worker's stderr is
    the same file: a worker whose turn quotes the phrase bubblewrap uses beside
    one of this launch's mount sources reads exactly like a refused sandbox. A
    try is dead only once its child has exited, so this launch keeps the worker
    it has and returns, rather than waiting on a child that never ended.
    """
    home, directory = isolated_run
    protected = home / ".claude.json"
    argv = _fenced_argv(home, directory, protected)
    source = str(_backends.resolved_destination(protected))
    phrase = f"bwrap: {REFUSALS[0]} {source} on {source}"
    quoting = _script(
        tmp_path / "quoting.sh",
        'echo \'{"type":"thread.started"}\'\n'
        f"echo {shlex.quote(phrase)} >&2\n"
        "sleep 60\n",
    )
    spawn = _recording_spawn(monkeypatch, [[str(quoting)]])

    try:
        started = time.monotonic()
        pid = _launch(argv, directory)
        elapsed = time.monotonic() - started
        assert len(spawn.processes) == 1, (
            "a running worker's own words were read as a refused launch"
        )
        assert pid == spawn.processes[0].pid
        assert spawn.processes[0].poll() is None, "the live worker was ended"
        record = runs.read_pointer(RUN_ID)
        assert record.get("launch_failures") in (None, [])
        assert elapsed < WINDOW, (
            "the launch never returned while its worker was still running"
        )
    finally:
        _cleanup(*spawn.processes)


def test_a_stream_record_ends_the_watch_on_the_healthy_path(
    tmp_path: Path,
    isolated_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch that reached its turn is not held to the window."""
    home, directory = isolated_run
    protected = home / ".claude.json"
    argv = _fenced_argv(home, directory, protected)
    worker = _script(
        tmp_path / "worker.sh",
        'sleep 0.2\necho \'{"type":"thread.started"}\'\nsleep 60\n',
    )
    spawn = _recording_spawn(monkeypatch, [[str(worker)]])

    try:
        started = time.monotonic()
        pid = _launch(argv, directory)
        elapsed = time.monotonic() - started
        assert len(spawn.processes) == 1
        assert pid == spawn.processes[0].pid
        assert (directory / "stream.jsonl").stat().st_size > 0
        assert elapsed < WINDOW, (
            "the launch waited the window out although its stream had begun"
        )
    finally:
        _cleanup(*spawn.processes)
