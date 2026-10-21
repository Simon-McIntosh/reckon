"""The current-code child is a server helper, not a crew worker launch.

The served child exists so a call that fails on a moved checkout is answered by
the checkout's own code. :func:`reckon.served_code.spawn_served_child` starts the
running interpreter with ``SERVED_CHILD_PROGRAM`` on ``-c``, and it is never
routed through the fleet gate that holds a paused fleet's worker launches — a
paused gate must not stop plan reads.

A crew worker launch is what :func:`reckon._backends.launch_plan` compiles.

These tests capture the child's argv by standing in for ``subprocess.Popen`` so
no process is started, then assert the argv's shape on its own, that its program
is the module's served-child program, and that neither the argv nor the program
text names a command a crew launch plan builds. A later change that points this
spawner at a worker launch fails here.
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


def test_the_served_child_starts_the_interpreter_with_a_c_program(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The argv's shape, asserted without naming the program text.

    The program constant is the only authority for its own text, so an assertion
    comparing it against itself moves with the code and pins nothing. The shape
    is asserted here on its own — the running interpreter, ``-c``, a non-empty
    program, the root — so a change to the executable or the argument layout
    reddens this case whatever the program text becomes.
    """
    root = tmp_path / "checkout"
    root.mkdir()
    captured = _capture_spawn(monkeypatch)

    served_code.spawn_served_child(root)

    assert len(captured) == 4, captured
    assert captured[0] == sys.executable, captured
    assert captured[1] == "-c", captured
    assert isinstance(captured[2], str) and captured[2].strip(), captured
    assert captured[3] == str(root), captured


def test_the_served_child_program_is_the_served_child_program(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The program the child runs is the module's own served-child program."""
    root = tmp_path / "checkout"
    root.mkdir()
    captured = _capture_spawn(monkeypatch)

    served_code.spawn_served_child(root)

    assert captured[2] == served_code.SERVED_CHILD_PROGRAM


def test_the_served_child_program_names_no_worker_command() -> None:
    """The program text itself carries no worker command a crew launch builds.

    A change that rewrote the program into a worker invocation reddens here as
    well as in the argv case, so the guard does not rest on one assertion.
    """
    worker_commands = _worker_commands()
    assert worker_commands, "no worker command compiled from a launch plan"
    program = served_code.SERVED_CHILD_PROGRAM
    assert isinstance(program, str) and program.strip()
    named = {Path(token).name for token in program.split()}
    assert not (worker_commands & named), worker_commands & named


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
