"""A worker that exits inside the supervisor's startup window keeps its status.

The spawn retry for a vanished bind source polls the worker it has just
spawned, and only a launch whose argv asks the fence to mount a read-only bind
source is polled at all. A poll of a child that has already exited collects its
status, and that status is then the only copy: the pid is no longer waitable.
Before the supervisor carried it, its own wait on that pid got ECHILD and the
attempt's exit record was written with ``exit_code`` and ``signal`` both null —
a worker that ran, and whose stream says so, recorded as if nothing had ended
it. These cases spawn a worker through the run's own supervisor, let it exit
inside the shipped ``SANDBOX_STARTUP_WINDOW_SECONDS``, and read the status off
the attempt's exit record.

Each case measures its own premise rather than assuming it: the control at the
top of the case runs the production startup poll over the same argv and fails
if the worker outlived the window, because a worker the poll never reaped would
be collected by the supervisor's own wait and the record would read the same
whether or not the poll's exit is carried. The launch used here exits in about
20 ms against the 500 ms window, so the premise holds by two orders of
magnitude rather than by a hair.
"""

from __future__ import annotations

import importlib
import json
import signal
import subprocess
import time
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RUN_ID = "r-early-exit"

# The chosen exit code of the first case's worker, so the record names a code
# no other part of this launch could have produced.
EARLY_EXIT_CODE = 7

# The signal the second case's worker ends its own process with, and the name
# the shell's kill builtin takes for it.
EARLY_EXIT_SIGNAL = signal.SIGTERM


@pytest.fixture()
def isolated_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """A temporary operator home, run directory and worktree, none shared."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (directory / "prompt.txt").write_text("exit at once\n", encoding="utf-8")
    # The dispatch path publishes the attempt before it starts the supervisor,
    # and the exit record is exposed under its canonical name for the current
    # attempt only.
    dispatch_module._prepare_attempt_records(
        directory,
        run_id=RUN_ID,
        attempt=1,
        attempt_kind="dispatch",
        attempt_started_at="2026-10-03T12:00:00Z",
    )
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "early-exit-fixture",
            "repo": str(worktree),
            "worktree": str(worktree),
            "backend": "probe",
            "launch": "cli",
            "dialect": "",
            "phase": "starting",
            "pid": None,
            "session": "coordinator-fixture",
            "manifest_path": str(directory / "manifest.md"),
            "log_path": str(directory / "stream.jsonl"),
            "stderr_path": str(directory / "stderr.log"),
            "attempt": 1,
            "attempt_kind": "dispatch",
            "attempt_started_at": "2026-10-03T12:00:00Z",
            "created_at": "2026-10-03T12:00:00Z",
            "node": {
                "id": "early-exit-node",
                "plan": "fixture",
                "time_budget": "20m",
                "manifest_path": str(directory / "manifest.md"),
            },
        },
    )
    return home, directory, worktree


def _fenced_argv(home: Path, directory: Path, worker: list[str]) -> list[str]:
    """Compose a fence the way a dispatch does, running ``worker`` behind it."""
    return _backends.fence_argv(
        worker,
        writable_directories=[str(directory)],
        home=str(home),
        config={"protected_paths": [str(home / ".claude.json")]},
    )


def _fence_shaped_argv(worker: list[str], source: Path) -> list[str]:
    """A launch argv in the shape the startup poll runs for, running ``worker``.

    The poll only runs for a launch whose argv asks the fence to mount a
    read-only bind source, so that shape is what puts a case inside the window.
    The worker runs directly here rather than under bubblewrap, which reports a
    worker it ends by signal as 128+signal: the signal case needs the record to
    name the signal the worker's own process was ended by.
    """
    return [*worker, "--ro-bind", str(source), str(source)]


def _stop(process: subprocess.Popen) -> None:
    """End a process this test started, and collect it."""
    process.kill()
    process.wait(timeout=10)


def _assert_reaped_inside_the_window(argv: list[str], directory: Path) -> None:
    """Prove the launch a case judges is reaped inside the shipped window.

    The case turns on the worker exiting inside the window: a worker that
    outlived the poll would be collected by the supervisor's own wait, and the
    record would read the same whether or not the poll's exit is carried. This
    runs the production poll over the same argv, so a case that has quietly
    stopped exercising the carried exit fails here rather than passing.
    """
    stderr_path = directory / "control.stderr.log"
    with open(stderr_path, "wb") as stderr:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            cwd=str(directory),
        )
    try:
        died_over_bind = dispatch_module._died_over_a_vanished_bind_source(
            process, argv=argv, stderr_path=stderr_path
        )
        assert not died_over_bind, "the control launch died over a bind source"
        assert process.returncode is not None, (
            "the control worker outlived the startup window, so this case would "
            "pass whether or not the poll's exit is carried"
        )
    finally:
        _stop(process)


def _supervise(argv: list[str], *, directory: Path, worktree: Path) -> dict:
    """Run one attempt under the run's own supervisor and read its exit record."""
    spec = dispatch_module._supervisor_spec(
        run_id=RUN_ID,
        run_directory=directory,
        repo_root=worktree,
        worktree=worktree,
        plan=_backends.LaunchPlan(
            backend="probe",
            dialect="",
            argv=argv,
            cwd=str(worktree),
            stdin_text="",
            environment={},
            final_message_path=None,
            resumed_session=None,
        ),
        prompt_path=directory / "prompt.txt",
        log_path=directory / "stream.jsonl",
        stderr_path=directory / "stderr.log",
        attempt=1,
        attempt_kind="dispatch",
        attempt_started_at="2026-10-03T12:00:00Z",
    )
    spec_path = directory / dispatch_module.SUPERVISOR_SPEC_NAME
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    # The supervisor installs its own stop handlers on the process it runs in
    # and never restores them, so a case that drives it here puts them back.
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        assert dispatch_module._run_supervisor(spec_path) == 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return json.loads(
        (directory / dispatch_module.EXIT_RECORD_NAME).read_text(encoding="utf-8")
    )


def test_a_worker_that_exits_inside_the_startup_window_keeps_its_code(
    isolated_run: tuple[Path, Path, Path],
) -> None:
    home, directory, worktree = isolated_run
    argv = _fenced_argv(home, directory, ["/bin/sh", "-c", f"exit {EARLY_EXIT_CODE}"])
    _assert_reaped_inside_the_window(argv, directory)

    record = _supervise(argv, directory=directory, worktree=worktree)

    assert record["exit_code"] == EARLY_EXIT_CODE
    assert record["signal"] is None
    assert record["signal_name"] is None


def test_a_worker_ended_by_a_signal_inside_the_startup_window_keeps_its_signal(
    isolated_run: tuple[Path, Path, Path],
) -> None:
    home, directory, worktree = isolated_run
    argv = _fence_shaped_argv(
        ["/bin/sh", "-c", f"kill -{EARLY_EXIT_SIGNAL.name.removeprefix('SIG')} $$"],
        home / ".claude.json",
    )
    _assert_reaped_inside_the_window(argv, directory)

    record = _supervise(argv, directory=directory, worktree=worktree)

    assert record["exit_code"] is None
    assert record["signal"] == int(EARLY_EXIT_SIGNAL)
    assert record["signal_name"] == EARLY_EXIT_SIGNAL.name


def test_a_carried_exit_stands_beside_another_child_of_the_supervisor(
    isolated_run: tuple[Path, Path, Path],
) -> None:
    """A child that is no part of the attempt cannot displace the worker's exit.

    The supervisor runs the attempt in the process this case runs in, so that
    process also holds whatever children ran here before it — an unreaped probe
    is enough, and one is planted here so the case does not depend on what ran
    earlier. The startup poll collected the worker's exit from the worker
    itself, and a child of the supervisor reaped afterwards belongs to no part
    of the attempt: it must not be taken as the exit that ended it.
    """
    home, directory, worktree = isolated_run
    argv = _fenced_argv(home, directory, ["/bin/sh", "-c", f"exit {EARLY_EXIT_CODE}"])
    _assert_reaped_inside_the_window(argv, directory)
    # Left unreaped on purpose: polling or waiting here would collect it, and
    # a child of the supervisor is the shape this case is about.
    leftover = subprocess.Popen(
        ["/bin/true"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.1)
    try:
        record = _supervise(argv, directory=directory, worktree=worktree)
        assert record["exit_code"] == EARLY_EXIT_CODE
        assert record["signal"] is None
    finally:
        leftover.wait(timeout=10)