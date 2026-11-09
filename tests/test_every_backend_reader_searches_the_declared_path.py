"""Every backend reader searches the directories the launch resolved.

A search path declared only in a project's own flight layer is honoured by the
pre-flight, so a launch that reaches composition must search it too. The
directories are resolved once, when the launch plan is built, and every reader
after that takes them from the plan: the worker's persisted PATH, the
supervisor spec a resume composes, and the run record a later attempt reads
them back from. A reader that resolved the flight layers again would see the
host layer alone and lose the project's declaration.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import dispatch_launch, dispatch_sessions

BACKEND = "stub-lane"
COMMAND = "clive"
PROJECT = "stub-project"


def _write_host_layer(tmp_path: Path) -> Path:
    """A host layer defining the backend with no declared search path."""
    layer = tmp_path / "host-flight.yaml"
    layer.write_text(
        "version: 1\n"
        "backends:\n"
        f"  {BACKEND}:\n"
        "    launch: cli\n"
        f"    command: {COMMAND}\n"
        "    environment:\n"
        "      PATH: /usr/bin:/bin\n",
        encoding="utf-8",
    )
    return layer


def _write_project_layer(tmp_path: Path, search_path: list[str]) -> Path:
    """A project layer adding only the declared search path."""
    state = tmp_path / "checkout" / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    layer = state / "flight.yaml"
    entries = "".join(f"      - {entry}\n" for entry in search_path)
    layer.write_text(
        f"version: 1\nbackends:\n  {BACKEND}:\n    search_path:\n" + entries,
        encoding="utf-8",
    )
    return layer


def _write_stub(directory: Path) -> Path:
    binary = directory / COMMAND
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _facts() -> SimpleNamespace:
    return SimpleNamespace(in_allocation=False)


@pytest.fixture(autouse=True)
def pinned_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH carrying no backend, so resolution is decided by the declaration."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


@pytest.fixture()
def project_declared_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory the project layer alone declares, holding the stub backend."""
    bindir = tmp_path / "project-backend-bin"
    bindir.mkdir()
    _write_stub(bindir)
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(_write_host_layer(tmp_path)))
    _write_project_layer(tmp_path, [str(bindir)])
    return bindir


def _project_backend(tmp_path: Path) -> dict[str, object]:
    """The project-resolved backend entry a dispatch holds when it composes."""
    resolved = flight.resolve(project=PROJECT, checkout_path=tmp_path / "checkout")
    return dict(resolved.config["backends"][BACKEND])


def _compose_launch(tmp_path: Path, backend: dict[str, object], prompt: str):
    """Compose a cli launch's plan the way a dispatch and a resume do."""
    from reckon import _backends

    plan = _backends.launch_plan(
        backend_name=BACKEND,
        backend=backend,
        prompt=prompt,
        worktree=str(tmp_path / "worktree"),
        manifest_path=str(tmp_path / "run" / "manifest.md"),
        final_message_path=str(tmp_path / "run" / "final.txt"),
        fence=False,
    )
    return dispatch_launch.resolve_launch_executable(plan, facts=_facts())


def _supervisor_path(plan, tmp_path: Path) -> str:
    """The PATH the supervisor hands the worker for one composed plan."""
    spec = dispatch_launch._supervisor_spec(
        run_id="r-test",
        run_directory=tmp_path / "run",
        repo_root=tmp_path,
        worktree=tmp_path / "worktree",
        plan=plan,
        prompt_path=tmp_path / "run" / "prompt.txt",
        log_path=tmp_path / "run" / "stream.jsonl",
        stderr_path=tmp_path / "run" / "stderr.log",
        facts=_facts(),
    )
    return str(spec["plan"]["environment"]["PATH"])


def test_a_first_launch_searches_a_project_layer_declaration(
    tmp_path: Path, project_declared_backend: Path
) -> None:
    """The plan resolves the stub, carries its directory, and the PATH has it."""
    plan = _compose_launch(tmp_path, _project_backend(tmp_path), "the brief")

    assert plan.argv[0] == str(project_declared_backend / COMMAND)
    assert plan.backend_search_path == (str(project_declared_backend),)
    assert str(project_declared_backend) in _supervisor_path(plan, tmp_path).split(
        os.pathsep
    )


def test_a_resume_searches_the_directories_the_record_carries(
    tmp_path: Path, project_declared_backend: Path
) -> None:
    """A resumed attempt reads the launched directories back from the record."""
    launched = _compose_launch(tmp_path, _project_backend(tmp_path), "the brief")
    record = {
        "run_id": "r-test",
        "backend": BACKEND,
        "launch": "cli",
        "command": COMMAND,
        "dialect": "clive",
        "backend_search_path": list(launched.backend_search_path),
    }

    # The layers reachable by name declare nothing, so a resumed attempt that
    # resolved them again would search no declared directory at all. The rebuild
    # from the record's own command drops every key that was not copied, so the
    # record is the only authority left for what this run searched.
    assert not dispatch_launch.declared_backend_search_path(
        flight.resolve().config["backends"].get(BACKEND)
    )
    rebuilt = dispatch_sessions._backend_settings(record, None)
    assert rebuilt["search_path"] == [str(project_declared_backend)]

    resumed = _compose_launch(tmp_path, rebuilt, "the advice")
    assert resumed.argv[0] == str(project_declared_backend / COMMAND)
    assert str(project_declared_backend) in _supervisor_path(resumed, tmp_path).split(
        os.pathsep
    )
