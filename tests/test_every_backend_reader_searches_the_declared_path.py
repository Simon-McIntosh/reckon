"""Every backend reader searches the directories the launch resolved.

A search path declared only in a project's own flight layer is honoured by the
pre-flight, so a launch that reaches composition must search it too. The
directories are resolved once, when the launch plan is built, and every reader
after that takes them from the launch's own run record: the worker's persisted
PATH, the supervisor spec a resume composes, and the record a lane change
rewrites. A reader that resolved the flight layers again would see the host
layer alone and lose the project's declaration.

Every case here drives the production path that writes the record — a dispatch
with a stand-in launcher, and the lane-change door itself — so deleting the
write each case measures makes it fail.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _backends, crew, flight
from reckon.crew import dispatch_launch, dispatch_sessions

BACKEND = "stub-lane"
DESTINATION = "declared-lane"
NATIVE = "harness-lane"
COMMAND = "clive"
PROJECT = "stub-project"
PLAN = "fixture"
LAUNCHED_PID = 4242
MOVED_PID = 43003


def _write_host_layer(tmp_path: Path, destination_bindir: Path) -> Path:
    """A host layer: the launch backend declares no path, the move's does."""
    layer = tmp_path / "host-flight.yaml"
    layer.write_text(
        "version: 1\n"
        "backends:\n"
        f"  {BACKEND}:\n"
        "    launch: cli\n"
        f"    command: {COMMAND}\n"
        "    sandbox: worktree-full\n"
        '    time_budget: "25m"\n'
        "    environment:\n"
        "      PATH: /usr/bin:/bin\n"
        f"  {DESTINATION}:\n"
        "    launch: cli\n"
        f"    command: {COMMAND}\n"
        "    sandbox: worktree-full\n"
        '    time_budget: "25m"\n'
        "    environment:\n"
        "      PATH: /usr/bin:/bin\n"
        "    search_path:\n"
        f"      - {destination_bindir}\n"
        f"  {NATIVE}:\n"
        "    launch: in-harness\n"
        "    sandbox: worktree-full\n"
        '    time_budget: "25m"\n',
        encoding="utf-8",
    )
    return layer


def _write_project_layer(repo: Path, search_path: list[str]) -> Path:
    """A project layer adding only the declared search path."""
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
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
def declared_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A throwaway repository whose project layer declares the launch's backend."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    source_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / f"{PLAN}.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="stub-project">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="movement">Backend movement</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )

    project_bindir = tmp_path / "project-backend-bin"
    project_bindir.mkdir()
    _write_stub(project_bindir)
    destination_bindir = tmp_path / "destination-backend-bin"
    destination_bindir.mkdir()
    _write_stub(destination_bindir)
    host_layer = _write_host_layer(tmp_path, destination_bindir)
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(host_layer))
    _write_project_layer(repo, [str(project_bindir)])
    return repo, project_bindir


def _resolved_config(repo: Path) -> dict[str, object]:
    """The project-resolved config a dispatch holds when it composes."""
    config = dict(flight.resolve(project=PROJECT, checkout_path=repo).config)
    config["default_backend"] = BACKEND
    config["roles"] = {"implement": {}}
    config["fences"] = {"time_budget": "25m", "needs_help_after_failures": 2}
    return config


def _node(tmp_path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="declared-path-node",
        goal="carry the backend's declared search directories through every launch",
        plan=PLAN,
        section="movement",
        spec_level="guided",
        done_when="uv run pytest tests/test_backends.py reports 34 passed",
        write_paths=["reckon/_backends.py"],
        time_budget="20m",
        manifest_path=str(tmp_path / "reports" / "worker.md"),
    )


def _launch(repo: Path, tmp_path: Path) -> dict:
    """Dispatch one cli run and return the record the launch wrote."""
    return crew.dispatch(
        node=_node(tmp_path),
        project=PROJECT,
        repo=repo,
        config=_resolved_config(repo),
        session="coordinator-session",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: LAUNCHED_PID,
    )


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


def _resumed_supervisor_path(
    backend: dict[str, object], expected_command: Path, tmp_path: Path
) -> str:
    plan = _backends.launch_plan(
        backend_name=BACKEND,
        backend=backend,
        prompt="the advice",
        worktree=str(tmp_path / "worktree"),
        manifest_path=str(tmp_path / "run" / "manifest.md"),
        final_message_path=str(tmp_path / "run" / "final.txt"),
        fence=False,
    )
    plan = dispatch_launch.resolve_launch_executable(plan, facts=_facts())
    assert plan.argv[0] == str(expected_command)
    return _supervisor_path(plan, tmp_path)


def test_a_first_launch_searches_a_project_layer_declaration(
    declared_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """The launch resolves the stub to the directory the project layer declares."""
    repo, project_bindir = declared_repo

    record = _launch(repo, tmp_path)

    assert record["backend_search_path"] == [str(project_bindir)]
    # The launch composes a fence, so the resolved command sits inside its argv
    # rather than at position zero.
    assert str(project_bindir / COMMAND) in record["argv"]


def test_a_resume_searches_the_directories_the_launch_recorded(
    declared_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """A resumed attempt reads the launched directories back from the record."""
    repo, project_bindir = declared_repo
    written = crew.read_pointer(_launch(repo, tmp_path)["run_id"])
    # The layers reachable by name declare nothing, so a resumed attempt that
    # resolved them again would search no declared directory at all. The rebuild
    # from the record's own command drops every key that was not copied, so the
    # record is the only authority left for what this launch searched.
    assert not dispatch_launch.declared_backend_search_path(
        flight.resolve().config["backends"].get(BACKEND)
    )
    assert written["backend_search_path"] == [str(project_bindir)]

    rebuilt = dispatch_sessions._backend_settings(written, None)
    assert rebuilt["search_path"] == [str(project_bindir)]

    supervisor_path = _resumed_supervisor_path(
        rebuilt, project_bindir / COMMAND, tmp_path
    )
    assert str(project_bindir) in supervisor_path.split(os.pathsep)


def test_a_lane_change_records_its_own_and_a_planless_lane_clears_them(
    declared_repo: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move records the destination's directories, and none for a planless lane."""
    repo, _project_bindir = declared_repo
    record = _launch(repo, tmp_path)
    run_id = record["run_id"]
    pointer = crew.read_pointer(run_id)
    pointer.update(
        {"phase": "working", "pid": LAUNCHED_PID, "pid_start_time": "started"}
    )
    crew._write_json(crew.pointer_path(run_id), pointer)
    monkeypatch.setattr(
        dispatch_sessions, "process_alive", lambda pid: pid in (LAUNCHED_PID, MOVED_PID)
    )
    monkeypatch.setattr(
        dispatch_sessions, "_signal_process_group", lambda *a, **k: None
    )
    config = _resolved_config(repo)
    destination = str(tmp_path / "destination-backend-bin")

    dispatch_sessions.change_lane(
        run_id,
        DESTINATION,
        "the destination lane is declared elsewhere",
        config=config,
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: MOVED_PID,
    )

    moved = crew.read_pointer(run_id)
    assert moved["backend"] == DESTINATION
    assert moved["backend_search_path"] == [destination]

    dispatch_sessions.change_lane(
        run_id,
        NATIVE,
        "the process backend cannot serve another turn",
        config=config,
    )

    assert crew.read_pointer(run_id)["backend_search_path"] is None
