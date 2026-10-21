"""The current-code child is a server helper, not a crew worker launch.

The served child exists so a call that fails on a moved checkout is answered by
the checkout's own code. :func:`reckon.served_code.spawn_served_child` starts the
running interpreter with ``SERVED_CHILD_PROGRAM`` on ``-c``, and it is never
routed through the fleet gate that holds a paused fleet's worker launches — a
paused gate must not stop plan reads.

A crew worker launch is what :func:`reckon._backends.launch_plan` compiles.

These tests capture the child's argv by standing in for ``subprocess.Popen`` so
no process is started, then assert two things: the argv is the interpreter with
the served-child program, and it names no command a crew launch plan builds.
A later change that points this spawner at a worker launch fails here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reckon import _backends, served_code

#: Backends a crew launch plan compiles, and so the worker commands to reject.
WORKER_BACKENDS = (
    {"launch": "cli", "command": "codex", "sandbox": "worktree-full"},
    {"launch": "cli", "command": "claude", "sandbox": "worktree-full"},
)


def _capture_spawn(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for ``subprocess.Popen`` and collect the argv it was given.

    Installed after any launch-plan compilation the test needs, because the
    stand-in answers no command and a compiled plan shells out to resolve its
    executable.
    """
    argv: list[str] = []

    def popen(command, **_kwargs):  # the Popen stand-in
        argv.extend(command)

    monkeypatch.setattr(served_code.subprocess, "Popen", popen)
    return argv


def _worker_commands() -> set[str]:
    """The executable basenames a crew launch plan builds for a worker."""
    commands = set()
    for backend in WORKER_BACKENDS:
        plan = _backends.launch_plan(
            backend_name="b",
            backend=backend,
            prompt="unused",  # only the compiled argv is read
            worktree="/wt",
        )
        commands.add(Path(plan.argv[0]).name)
    return commands


def test_the_served_child_starts_the_interpreter_with_its_program(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    captured = _capture_spawn(monkeypatch)

    served_code.spawn_served_child(root)

    assert captured == [
        sys.executable,
        "-c",
        served_code.SERVED_CHILD_PROGRAM,
        str(root),
    ]


def test_the_served_child_argv_names_no_worker_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    # Compile the worker commands first: the stand-in answers no subprocess, so
    # a launch plan built while it is installed cannot resolve its executable.
    worker_commands = _worker_commands()
    # Guard the measurement: the rejection is only meaningful when a launch plan
    # actually names a command to reject.
    assert worker_commands, "no worker command compiled from a launch plan"
    captured = _capture_spawn(monkeypatch)

    served_code.spawn_served_child(root)

    named = {Path(token).name for token in captured}
    assert not (worker_commands & named), (worker_commands & named, captured)
