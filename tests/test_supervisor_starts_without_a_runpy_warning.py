"""Gate: the per-run supervisor launches through a quiet entry module.

Dispatch starts the supervisor as ``python -m reckon.crew.supervisor_main``.
Running dispatch itself as the main module instead re-executes a module the
crew package imported when it loaded, and runpy opens the supervisor's stderr
with a duplicate-import ``RuntimeWarning`` before any supervisor code runs.
The warning is harmless, but it is the first line a reader sees when
diagnosing a dead launch.

Three facts hold the fix. The launch vector names the entry module. A launched
supervisor's stderr log is empty. And no module under ``reckon/`` imports the
entry module, which is what keeps the runpy check from finding it already in
``sys.modules``.

The cases synthesise a run in a throwaway ``RECKON_HOME`` with a stub worker
and launch the supervisor through the vector dispatch builds, so the stderr
they read is a real launch's. A positive control launches the legacy module
over the same run and shows the warning the empty-stderr case must catch, and
a negative control (the vector pointed back at the legacy module) turns that
case red -- both keep the empty reading from being an absence of measurement.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import reckon
from reckon import _backends

# The package the cases exercise, resolved from the interpreter running them
# rather than from the repository path, because the gate also runs against a
# copy of the package carrying a deliberate defect and must then measure that
# copy.
PACKAGE_ROOT = Path(reckon.__file__).resolve().parents[1]

# The module under test, imported as the gate's own package so the vector is
# read from the tree this case imported rather than any other.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

ENTRY_MODULE = "reckon.crew.supervisor_main"
LEGACY_MODULE = "reckon.crew.dispatch"
RUN_ID = "r-supervisor-stderr"
SUPERVISOR_STDERR_NAME = "supervisor.stderr.log"
LAUNCH_BOUND = 60.0


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway crew home, so the launch touches no real fleet state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _synthesise_run(home: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write one run's pointer and supervisor spec with a stub worker.

    Returns the run directory, the spec path, and the marker the stub worker
    writes, so a case can prove the supervisor reached its run and spawned.
    """
    run_directory = home / "crew" / "runs" / RUN_ID
    run_directory.mkdir(parents=True)
    # The attempt marker the supervisor reads before it exposes an attempt's
    # current records at their canonical names, as a launched run carries.
    (run_directory / dispatch_module.ATTEMPT_RECORD_NAME).write_text(
        json.dumps({"attempt": 1}), encoding="utf-8"
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    prompt_path = run_directory / "prompt.txt"
    prompt_path.write_text("stub prompt\n", encoding="utf-8")
    marker = run_directory / "worker.marker"
    plan = _backends.LaunchPlan(
        backend="stub",
        dialect="generic",
        argv=[
            sys.executable,
            "-c",
            "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')",
            str(marker),
        ],
        cwd=str(worktree),
        stdin_text="",
        environment={},
        final_message_path=None,
        resumed_session=None,
    )
    spec = dispatch_module._supervisor_spec(
        run_id=RUN_ID,
        run_directory=run_directory,
        repo_root=worktree,
        worktree=worktree,
        plan=plan,
        fenced=False,
        prompt_path=prompt_path,
        log_path=run_directory / "stream.jsonl",
        stderr_path=run_directory / "stderr.log",
    )
    spec_path = run_directory / "supervisor.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    pointer = {
        "run_id": RUN_ID,
        "project": "sample",
        "repo": str(worktree),
        "worktree": str(worktree),
        "backend": "stub",
        "launch": "cli",
        "dialect": "generic",
        "phase": "running",
        "pid": None,
        "session": "gate-session",
        "manifest_path": str(run_directory / "manifest.md"),
    }
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / f"{RUN_ID}.json").write_text(json.dumps(pointer), encoding="utf-8")
    return run_directory, spec_path, marker


def _launch(run_directory: Path, vector: list[str]) -> tuple[int, str]:
    """Run one supervisor vector and return its exit status and stderr text."""
    log = run_directory / SUPERVISOR_STDERR_NAME
    environment = {
        **os.environ,
        "PYTHONPATH": str(PACKAGE_ROOT),
        "RECKON_FLEET_SPAWN": "",
    }
    with log.open("wb") as errors:
        completed = subprocess.run(
            vector,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            env=environment,
            timeout=LAUNCH_BOUND,
            check=False,
        )
    return completed.returncode, log.read_text(encoding="utf-8", errors="replace")


def test_the_launch_vector_names_the_entry_module() -> None:
    """The vector dispatch builds runs the entry module, not dispatch itself."""
    vector = dispatch_module._supervisor_argv(spec_path=Path("supervisor.json"))
    assert vector[:3] == [sys.executable, "-m", ENTRY_MODULE], vector
    assert LEGACY_MODULE not in vector, vector


def test_a_launched_supervisor_writes_no_stderr(
    crew_home: Path, tmp_path: Path
) -> None:
    """A real launch reaches its run, spawns its worker, and opens no stderr."""
    run_directory, spec_path, marker = _synthesise_run(crew_home, tmp_path)
    vector = dispatch_module._supervisor_argv(spec_path=spec_path)
    returncode, stderr_text = _launch(run_directory, vector)
    assert returncode == 0, f"the supervisor exited {returncode}: {stderr_text!r}"
    # The positive control for the empty reading: a supervisor that never
    # reached its run would also write no stderr, so the spawn must be shown.
    worker = json.loads((run_directory / "worker.json").read_text(encoding="utf-8"))
    assert int(worker["pid"]) != os.getpid()
    assert marker.exists(), "the stub worker never ran, so nothing was spawned"
    assert stderr_text == "", (
        f"the supervisor's stderr opened with: {stderr_text!r}"
    )


def test_the_legacy_entry_module_opens_stderr_with_the_warning(
    crew_home: Path, tmp_path: Path
) -> None:
    """The instrument reads the defect: dispatch as main still warns.

    Without this the empty-stderr case above cannot distinguish a quiet launch
    from a stderr nobody read.
    """
    run_directory, spec_path, _marker = _synthesise_run(crew_home, tmp_path)
    vector = dispatch_module._supervisor_argv(spec_path=spec_path)
    legacy = [LEGACY_MODULE if token == ENTRY_MODULE else token for token in vector]
    assert LEGACY_MODULE in legacy
    returncode, stderr_text = _launch(run_directory, legacy)
    assert returncode == 0, f"the legacy launch failed: {stderr_text!r}"
    assert "RuntimeWarning" in stderr_text and ENTRY_MODULE not in stderr_text, (
        "the legacy module did not open stderr with the duplicate-import "
        f"warning, so the empty-stderr case measures nothing: {stderr_text!r}"
    )


def _modules_importing_the_entry_module() -> list[str]:
    """Every module under ``reckon/`` that imports the entry module."""
    importers: list[str] = []
    for source in sorted((PACKAGE_ROOT / "reckon").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        if _imports_entry_module(tree):
            importers.append(str(source.relative_to(PACKAGE_ROOT)))
    return importers


def _imports_entry_module(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == ENTRY_MODULE or alias.name.endswith(".supervisor_main")
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == ENTRY_MODULE or module.endswith(".supervisor_main"):
                return True
        elif isinstance(node, ast.Call):
            callee = node.func
            name = getattr(callee, "attr", None) or getattr(callee, "id", None)
            if name in {"import_module", "__import__"} and node.args:
                argument = node.args[0]
                if (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                    and "supervisor_main" in argument.value
                ):
                    return True
    return False


def test_no_module_under_reckon_imports_the_entry_module() -> None:
    """Nothing imports the entry module, so runpy cannot find it pre-imported."""
    importers = _modules_importing_the_entry_module()
    assert importers == [], (
        f"these modules import {ENTRY_MODULE}, which re-exposes the supervisor "
        f"launch to the runpy duplicate-import warning: {importers}"
    )
