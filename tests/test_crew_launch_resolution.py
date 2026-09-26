"""Every launch resolves its backend to an absolute path, or refuses first.

The fault: a launch plan carried a bare backend name and inherited the PATH of
whoever built it, so a watcher armed without the backend directory exec'd a
missing binary, died at exec, and left a 0-byte stream per tick while the run
read as working. Each surface here is exercised with the backend directory
removed from the PATH *handed to the call* — the plan's own environment, or the
project's declared backend environment — never from the test process, which
keeps its own PATH so the assertions themselves can still run.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

MISSING_PATH = "/nonexistent-backend-bin"


def _backend_environment(path: str) -> dict:
    return {"PATH": path}


def _config(environment: dict) -> dict:
    return {
        "default_backend": "alpha",
        "backends": {
            "alpha": {
                "launch": "cli",
                "command": "codex",
                "model": "some-model",
                "effort": "high",
                "sandbox": "worktree-full",
                "session_reuse": True,
                "time_budget": "25m",
                "environment": environment,
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A hermetic mount carrying a plan, a git checkout and a flight config."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(name: str, manifest_path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="resolve the backend before the launch writes anything",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="one launch resolution case passes",
        write_paths=["src/resolved.txt"],
        time_budget="20m",
        manifest_path=manifest_path,
    )


def _admit_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the seat only; delivery is read from the real registry."""
    watcher = {"arming_line": "reckon crew watch --project sample", "watcher": {}}

    def watch_state(_project: str, *, session: str | None = None) -> dict:
        delivery = (
            runs.follower_state(_project, session) if session is not None else None
        )
        return {
            **watcher,
            "attach_line": runs._watch_attach_line(_project, session=session),
            "watcher_live": True,
            "session": session,
            "session_attached": None if delivery is None else bool(delivery["live"]),
            "follower": {} if delivery is None else delivery["follower"],
        }

    monkeypatch.setattr(dispatch_module, "watch_state", watch_state)


def _dispatch(
    repo: Path,
    config: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    launcher=None,
) -> dict:
    _admit_watcher(monkeypatch)
    session = "session-resolve"
    seen: dict = {}

    def capture(plan, **_kwargs):
        seen["plan"] = plan
        return launcher(plan) if launcher is not None else 4242

    with runs.follower_claim("sample", session, delivery="stream"):
        crew.dispatch(
            # The node contract requires a manifest path the orchestrator can
            # reach, so it is absolute and outside the worker's worktree.
            node=_node("resolve", str(repo.parent / "manifest.md")),
            project="sample",
            repo=repo,
            config=config,
            session=session,
            launcher=capture,
            watch_required=False,
        )
    return seen


def _harness_executable(plan) -> str:
    """The element of a launch plan the worker will actually run.

    A fenced plan leads with the fence binary and carries the harness behind
    that fence's own ``--`` separator; an unfenced plan's harness is its first
    element. The position is read through the module's own helper, so this reads
    the same element the resolution rewrites.
    """
    argv = list(plan.argv)
    return str(argv[dispatch_module.harness_command_index(argv)])


def test_a_dispatch_plan_carries_an_absolute_executable(
    project: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config_home, repo = project
    bin_dir = tmp_path / "backend-bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)

    seen = _dispatch(repo, _config(_backend_environment(str(bin_dir))), monkeypatch)

    executable = _harness_executable(seen["plan"])
    assert Path(executable).is_absolute()
    assert Path(executable) == fake.resolve()
    assert Path(executable).name == "codex"


def test_a_launcher_reached_through_a_symlink_keeps_its_own_name(
    project: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absolute, not canonical: the name is how the command's dialect is read.

    A launcher installed as ``bin/codex`` symlinked to ``codex.js`` was recorded
    under the target's name once the path was canonicalised, and the recorded
    command then matched no dialect, so the run's own stream could no longer be
    read back. The absolute path has to keep the name the launch was configured
    with.
    """
    _config_home, repo = project
    bin_dir = tmp_path / "backend-bin"
    bin_dir.mkdir()
    target = bin_dir / "codex.js"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    (bin_dir / "codex").symlink_to(target)

    seen = _dispatch(repo, _config(_backend_environment(str(bin_dir))), monkeypatch)

    executable = Path(_harness_executable(seen["plan"]))
    assert executable.is_absolute()
    assert executable == bin_dir / "codex"
    assert executable.name == "codex"


def test_a_dispatch_refuses_an_unresolvable_backend_before_writing_anything(
    project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config_home, repo = project

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(repo, _config(_backend_environment(MISSING_PATH)), monkeypatch)

    message = str(refusal.value)
    assert "codex" in message
    assert MISSING_PATH in message
    assert list(runs.live_dir().glob("*.json")) == []
    assert not runs.runs_dir().exists() or list(runs.runs_dir().iterdir()) == []


def test_a_resume_refuses_an_unresolvable_backend_and_leaves_the_pointer_alone(
    project: tuple[Path, Path],
) -> None:
    _config_home, repo = project
    run_id = "r-resolve-refusal"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    worktree = repo / "worktree"
    worktree.mkdir()
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "sample",
            "repo": str(repo),
            "phase": "working",
            "backend": "alpha",
            "launch": "cli",
            "pid": None,
            "session_id": "session-abc",
            "worktree": str(worktree),
            "manifest_path": str(directory / "manifest.md"),
            "log_path": str(directory / "stream.jsonl"),
            "stderr_path": str(directory / "stderr.log"),
            "node": {"id": "node-resolve", "plan": "fixture"},
        },
    )
    before = runs.pointer_path(run_id).read_bytes()

    with pytest.raises(crew.CrewError) as refusal:
        dispatch_module.resume_plan(
            run_id, "continue", config=_config(_backend_environment(MISSING_PATH))
        )

    message = str(refusal.value)
    assert "codex" in message
    assert MISSING_PATH in message
    # The refusal precedes the pointer write, the advice file and the stream.
    assert runs.pointer_path(run_id).read_bytes() == before
    assert sorted(path.name for path in directory.iterdir()) == []


def test_a_watcher_refuses_to_arm_when_a_routable_backend_is_missing(
    project: tuple[Path, Path],
) -> None:
    config_home, repo = project
    state = repo / "docs" / "state" / "sample"
    state.mkdir(parents=True)
    (state / "flight.yaml").write_text(
        "default_backend: alpha\n"
        "backends:\n"
        "  alpha:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    environment:\n"
        f"      PATH: {MISSING_PATH}\n",
        encoding="utf-8",
    )
    from reckon.crew import recovery

    with pytest.raises(crew.CrewError) as refusal:
        next(recovery.watch_ticker("sample", poll_interval=0))

    message = str(refusal.value)
    assert "codex" in message
    assert MISSING_PATH in message
    # The seat is never taken by a watcher that cannot lift.
    assert not (config_home / "crew" / "watch").exists()


def test_the_once_watch_arm_refuses_the_seat_when_a_routable_backend_is_missing(
    project: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """The single-event arm takes the same seat, so it owes the same refusal.

    ``crew watch --once`` is the single-event arm of the seat the streaming
    watcher holds. It claimed the seat with no resolution check on its path, so
    a host whose backend directory is off the project's PATH could arm a
    seat-holder that reads as live and can lift nothing from it.
    """
    config_home, repo = project
    state = repo / "docs" / "state" / "sample"
    state.mkdir(parents=True)
    flight_yaml = state / "flight.yaml"
    flight_yaml.write_text(
        "default_backend: alpha\n"
        "backends:\n"
        "  alpha:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    environment:\n"
        f"      PATH: {MISSING_PATH}\n",
        encoding="utf-8",
    )

    with pytest.raises(crew.CrewError) as refusal:
        runs.watch("sample", exit_on_empty=True, poll_interval=0)

    message = str(refusal.value)
    assert "codex" in message
    assert MISSING_PATH in message
    # No lock file is created, so no watcher reads as armed on this host.
    assert not (config_home / "crew" / "watch").exists()

    # Positive control: the same call with the backend resolvable does reach the
    # claim and take the seat, so the absence above is the refusal rather than a
    # call that never got as far as claiming anything.
    bin_dir = tmp_path / "backend-bin"
    bin_dir.mkdir()
    launcher = bin_dir / "codex"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    flight_yaml.write_text(
        "default_backend: alpha\n"
        "backends:\n"
        "  alpha:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    environment:\n"
        f"      PATH: {bin_dir}\n",
        encoding="utf-8",
    )

    runs.watch("sample", exit_on_empty=True, poll_interval=0)

    assert list((config_home / "crew" / "watch").glob("*.lock"))
