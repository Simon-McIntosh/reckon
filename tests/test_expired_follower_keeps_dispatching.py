"""A session whose follower expired keeps dispatching, warned to re-arm.

A follower registration keeps its file when its lock is released, so a session
that armed one and then lost it is distinguishable from one that never armed
one at all: the release leaves a record, the never-registered session leaves
nothing. The dispatch guard refuses only the second while a live watcher
process keeps the project covered — the first proceeds with a warning carrying
the exact attach command that restores delivery. The dry run, which never
launches anything, reaches the same two verdicts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs

# The suite suppresses watcher arming by default; these cases decide on the
# watcher, so they run with arming allowed and register their own watcher
# process rather than starting a producer.
pytestmark = pytest.mark.arms_watch_producer

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
        goal="record dispatch admission for one expired follower",
        plan="fixture",
        section="guard",
        spec_level="exact",
        done_when="pytest reports one passing expired-follower dispatch case",
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


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


def _release_follower(project: str, session: str) -> None:
    """Arm a follower and let it release, leaving the file a release leaves."""
    with runs.follower_registration(project, session, delivery="stream"):
        pass


def test_dispatch_admits_a_released_follower_with_a_rearm_warning(
    isolated_project: tuple[Path, Path],
) -> None:
    """A released registration keeps dispatching, and is told to re-arm."""
    config_home, repo = isolated_project
    project = "sample"
    session = "session-released"
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        _release_follower(project, session)
        # The registration is released, not absent: the file records it while
        # its advisory lock is free.
        released = runs.follower_state(project, session)
        assert released["registered"] is False
        assert released["released"] is True, "the release must leave a record"

        record = crew.dispatch(
            node=_node(config_home, "released"),
            project=project,
            repo=repo,
            config=CONFIG,
            session=session,
            launcher=lambda *args, **kwargs: 4242,
            watch_required=True,
        )
        assert record["watch"]["watcher_live"] is True
        assert record["watch"]["session_attached"] is False
        assert record["watch"]["session_follower_released"] is True
        warnings = [str(item) for item in record["warnings"]]
        attach_line = runs.watch_state(project, session=session)["attach_line"]
        assert any(attach_line in item for item in warnings), warnings
    finally:
        runner.terminate()
        runner.wait(timeout=5)


def test_dispatch_refuses_a_never_registered_follower(
    isolated_project: tuple[Path, Path],
) -> None:
    """A session that never armed a follower is still refused."""
    config_home, repo = isolated_project
    project = "sample"
    session = "session-never"
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        # No release, no registration file: this session never armed one.
        assert not runs.follower_lock_path(project, session).is_file()

        with pytest.raises(crew.WatcherRequired) as refusal:
            crew.dispatch(
                node=_node(config_home, "never"),
                project=project,
                repo=repo,
                config=CONFIG,
                session=session,
                launcher=lambda *args, **kwargs: 4242,
                watch_required=True,
            )
        assert not list(crew.list_live(project=project)), "nothing may be created"
        assert session in str(refusal.value)
    finally:
        runner.terminate()
        runner.wait(timeout=5)


def test_dry_run_reports_the_released_follower_proceeding(
    isolated_project: tuple[Path, Path],
) -> None:
    """The validating path flags the released registration and proceeds."""
    config_home, repo = isolated_project
    project = "sample"
    session = "session-released-dry"
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        _release_follower(project, session)
        resolution = crew.plan_dispatch(
            node=_node(config_home, "released-dry"),
            config=CONFIG,
            project=project,
            repo=repo,
            session=session,
            watch_required=True,
        )
        warnings = [str(item) for item in resolution.warnings]
        expected = runs.watch_state(project, session=session)["attach_line"]
        assert any(expected in item for item in warnings), warnings
    finally:
        runner.terminate()
        runner.wait(timeout=5)


def test_dry_run_refuses_a_never_registered_follower(
    isolated_project: tuple[Path, Path],
) -> None:
    """The validating path refuses a session with no registration to release."""
    config_home, repo = isolated_project
    project = "sample"
    session = "session-never-dry"
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        with pytest.raises(crew.WatcherRequired):
            crew.plan_dispatch(
                node=_node(config_home, "never-dry"),
                config=CONFIG,
                project=project,
                repo=repo,
                session=session,
                watch_required=True,
            )
    finally:
        runner.terminate()
        runner.wait(timeout=5)
