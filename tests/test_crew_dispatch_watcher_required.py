"""A dispatch is admitted by a watcher process, never by a session follower.

A seat is project-global and delivery is session-local, so a project can hold a
live follower of the dispatching session and still have no watcher process at
all. Liveness is read from the process, and the refusal for a project that has
none names the command that starts one the operator owns.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs

CONFIG = {
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
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A mountable repository plus a configuration home, as dispatch needs."""
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


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="record watcher admission for one dispatch",
        plan="fixture",
        section="guard",
        spec_level="exact",
        done_when="pytest reports one passing watcher-required dispatch case",
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _dispatch(config_home: Path, repo: Path, name: str) -> dict:
    """Dispatch with this session's delivery registered, as a coordinator does."""
    session = f"session-{name}"
    with runs.follower_claim("sample", session, delivery="stream"):
        return crew.dispatch(
            node=_node(config_home, name),
            project="sample",
            repo=repo,
            config=CONFIG,
            session=session,
            launcher=lambda *args, **kwargs: 4242,
            watch_required=True,
        )


class _ExitedProducer:
    """A producer that has already ended, so no watcher process comes up."""

    def poll(self) -> int:
        return 0


def _spawn_runner() -> subprocess.Popen[str]:
    """A long-lived process used as a genuinely running registered watcher."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _register_watcher(project: str, pid: int) -> None:
    """Register a running process as the project's watcher seat."""
    path = crew.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        crew._write_watch_record(
            handle,
            {"pid": pid, "pid_start_time": crew._process_start_time(pid)},
        )


@pytest.mark.arms_watch_producer
def test_dispatch_refuses_a_follower_when_no_watcher_process_exists(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live follower is delivery; the watcher requirement wants a process."""
    import importlib

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    config_home, repo = isolated_project
    monkeypatch.setattr(
        dispatch_module, "_start_watch_producer", lambda project: _ExitedProducer()
    )

    with pytest.raises(crew.WatcherRequired) as refusal:
        _dispatch(config_home, repo, "follower-only")

    message = str(refusal.value)
    assert runs.watcher_ensure_line("sample") in message
    # The refusal names the durable repair, and the run it refused created
    # nothing: no worktree, no pointer.
    assert "reckon crew watch --ensure --project sample" in message
    assert not list(crew.list_live(project="sample")), "nothing may be created"


@pytest.mark.arms_watch_producer
def test_dispatch_admits_a_project_with_a_live_watcher_process(
    isolated_project: tuple[Path, Path],
) -> None:
    """The same dispatch, with a watcher process, is admitted unchanged."""
    config_home, repo = isolated_project
    runner = _spawn_runner()
    try:
        _register_watcher("sample", runner.pid)
        record = _dispatch(config_home, repo, "watched")
        assert record["watch"]["watcher_live"] is True
        assert record["watch"]["watcher"]["pid"] == runner.pid
        assert record["watch"]["session_attached"] is True
    finally:
        runner.terminate()
        runner.wait(timeout=5)
