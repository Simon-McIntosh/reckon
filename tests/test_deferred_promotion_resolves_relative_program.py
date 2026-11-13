"""A deferred promotion resolves a relative gate program against the run.

A worker records the gate command it ran, and the repository's own recipe
writes that program as a path relative to the tree: ``.venv/bin/python``. The
check that refuses a command naming no program once resolved that path with
``Path(program).is_file()``, which reads the promoting process's working
directory. When ``crew complete`` promoted in the caller's process that
directory was the checkout and the check passed; a promotion deferred to the
admission service runs from the crew home, where the same relative program
names nothing and a reviewed run is refused for the process's location rather
than for its command.

Each case below drives ``_require_executable_gate_command`` directly, with the
process working directory moved to a directory unrelated to the run, and
asserts both directions: a program present in the run's checkout resolves, and
one absent from it is refused with a message naming the directory it was
resolved against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.promotion_gate import _require_executable_gate_command
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "proj"
RUN_ID = "r-20260101T000000000000-relative-program"
GATE_COMMAND = "PYTHONPATH=. .venv/bin/python -m pytest -q tests/x.py"


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def run_checkout(tmp_path: Path) -> Path:
    """A run's checkout, holding the interpreter its recorded command names."""
    checkout = tmp_path / "run-worktree"
    interpreter = checkout / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(interpreter.stat().st_mode | 0o111)
    (checkout / "tests").mkdir()
    (checkout / "tests" / "x.py").write_text("", encoding="utf-8")
    return checkout


def _record_run(repository: Path) -> None:
    """Write the live pointer a deferred promotion resolves the run through."""
    directory = run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.md").write_text("node: relative-program-fixture\n")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "role": "implement",
            "launch": "in-harness",
            "created_at": "2026-01-01T00:00:00Z",
            "node": {
                "id": "relative-program-fixture",
                "plan": "plan-a",
                "section": "relative-program",
                "write_paths": [],
            },
        },
    )


def test_a_relative_program_resolves_against_the_runs_checkout(
    crew_home: Path,
    run_checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The promoting process's directory is elsewhere; the run's checkout has it."""
    _record_run(run_checkout)
    unrelated = tmp_path / "promoting-process-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    _require_executable_gate_command(RUN_ID, {"command": GATE_COMMAND})


def test_a_relative_program_the_checkout_lacks_is_refused_naming_the_checkout(
    crew_home: Path,
    run_checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (run_checkout / ".venv" / "bin" / "python").unlink()
    _record_run(run_checkout)
    unrelated = tmp_path / "promoting-process-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    with pytest.raises(CrewError) as refusal:
        _require_executable_gate_command(RUN_ID, {"command": GATE_COMMAND})

    assert str(run_checkout) in str(refusal.value)


def test_an_absolute_program_resolves_without_the_checkout(
    crew_home: Path,
    run_checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A program naming an existing absolute executable is still accepted."""
    _record_run(run_checkout)
    monkeypatch.chdir(run_checkout)
    _require_executable_gate_command(RUN_ID, {"command": "/bin/sh -c 'exit 0'"})


def test_a_program_on_path_resolves_without_the_checkout(
    crew_home: Path,
    run_checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare name is looked up on PATH, which keeps naming a program."""
    _record_run(run_checkout)
    monkeypatch.chdir(run_checkout)
    _require_executable_gate_command(RUN_ID, {"command": "sh -c 'exit 0'"})
