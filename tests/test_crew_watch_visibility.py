"""Hermetic read-surface coverage for project watcher visibility."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, mcp
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


def _read_cli(project: str) -> dict:
    result = CliRunner().invoke(cli_module.main, ["crew", "list", "--project", project])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["watcher"]


def _arm_watcher(project: str, source_root: Path) -> subprocess.Popen[str]:
    script = f"""
import time
from reckon.crew import _project_watch_claim
with _project_watch_claim({project!r}, '1h') as (acquired, record):
    print('ready' if acquired else 'refused', flush=True)
    if acquired:
        time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=source_root,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def test_watcher_visibility_tracks_registration_liveness(tmp_path, monkeypatch) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    project = "synthetic-project"
    crew._write_json(
        crew.pointer_path("live-run"),
        {
            "run_id": "live-run",
            "project": project,
            "phase": "working",
            "node": {"id": "worker", "plan": "delivery"},
        },
    )

    unwatched = mcp._crew(project, view="live")["watcher"]
    assert unwatched == _read_cli(project)
    assert unwatched["status"] == "unwatched"
    assert unwatched["seat_held"] is False
    assert unwatched["watcher_live"] is False
    assert unwatched["pointer_count"] == 1
    assert unwatched["arming_line"] == (f"reckon crew watch --project {project}")

    process = _arm_watcher(project, Path(__file__).parents[1])
    try:
        watched = mcp._crew(project, view="live")["watcher"]
        assert watched == _read_cli(project)
        assert watched["status"] == "watched"
        assert watched["seat_held"] is True
        assert watched["watcher_live"] is True
        assert watched["pid"] == process.pid
        assert watched["armed_at"]
        assert watched["process_alive"] is True
    finally:
        process.terminate()
        process.wait(timeout=5)

    # Reconcile-on-read: the first read after the process dies repairs the
    # stale registration in place, so this read (and every later one) sees the
    # cleared seat rather than the dead pid the record claimed.
    stale = mcp._crew(project, view="live")["watcher"]
    assert stale == _read_cli(project)
    assert stale["status"] == "unwatched"
    assert stale["seat_held"] is False
    assert stale["watcher_live"] is False
    assert stale["pid"] is None
    assert stale["armed_at"] is None
    assert stale["process_alive"] is None
    assert stale["arming_line"] == unwatched["arming_line"]
    assert crew._read_watch_record(crew.watch_lock_path(project).open("rb")) == {}


def _write_seat_record(project: str, record: dict) -> None:
    """Write a watcher seat record without taking its advisory lock."""
    path = crew.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        crew._write_watch_record(handle, record)


def _hold_seat_lock(project: str, record: dict):
    """Write a seat record while exclusively holding its lock; caller closes."""
    path = crew.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    crew._write_watch_record(handle, record)
    return handle


def _spawn_runner() -> subprocess.Popen[str]:
    """A long-lived process used to desync the registry against the machine."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dead_pid_start() -> tuple[int, str]:
    """A pid + start time that no longer name a running process."""
    process = _spawn_runner()
    start = crew._process_start_time(process.pid)
    process.terminate()
    process.wait(timeout=5)
    assert crew.process_alive(process.pid) is False
    return process.pid, str(start)


def _setup_runs_home(tmp_path: Path, monkeypatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A mountable repo + config for dispatching through the real guard."""
    config_home = _setup_runs_home(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-ship" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
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
        goal="record watcher state for one dispatch",
        plan="fixture",
        section="guard",
        done_when="pytest reports one passing watcher guard case",
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


def test_watch_state_reconciles_liveness_against_the_process_table(
    tmp_path: Path, monkeypatch
) -> None:
    """The guard's value follows the process, not the seat, in both directions."""
    _setup_runs_home(tmp_path, monkeypatch)
    project = "desync-project"

    # A seat held by a dead process claims a watcher that does not exist; the
    # lock probe would read it as live.
    pid, start = _dead_pid_start()
    held = _hold_seat_lock(project, {"pid": pid, "pid_start_time": start})
    try:
        state = crew.watch_state(project)
        assert state["watcher_live"] is False
        assert state["watcher"]["pid"] == pid
    finally:
        held.close()

    # A lock that is not held reuses a registration whose process is running;
    # the lock probe would read it as absent.
    runner = _spawn_runner()
    try:
        _write_seat_record(
            project,
            {"pid": runner.pid, "pid_start_time": crew._process_start_time(runner.pid)},
        )
        state = crew.watch_state(project)
        assert state["watcher_live"] is True
        assert state["watcher"]["pid"] == runner.pid
    finally:
        runner.terminate()
        runner.wait(timeout=5)


def test_read_reconciles_a_stale_record_in_place_and_never_denies_an_arming(
    tmp_path: Path, monkeypatch
) -> None:
    """Reading liveness repairs a desync and leaves the seat claimable."""
    _setup_runs_home(tmp_path, monkeypatch)
    project = "repair-project"
    pid, start = _dead_pid_start()
    _write_seat_record(project, {"pid": pid, "pid_start_time": start})

    visibility = crew.project_watch_visibility(project)
    assert visibility["watcher_live"] is False
    assert visibility["process_alive"] is None  # repaired: no registered process

    # The desync is repaired, not merely reported: the record is now empty.
    assert crew._read_watch_record(crew.watch_lock_path(project).open("rb")) == {}

    # Observing never denied an arming: a fresh watcher still claims the seat.
    process = _arm_watcher(project, Path(__file__).parents[1])
    try:
        assert crew.watch_state(project)["watcher_live"] is True
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_dispatch_refuses_when_the_stale_record_claims_a_live_watcher(
    isolated_project: tuple[Path, Path],
) -> None:
    """A producer that is gone is refused, although the record claims it is live."""
    config_home, repo = isolated_project
    pid, start = _dead_pid_start()
    held = _hold_seat_lock("sample", {"pid": pid, "pid_start_time": start})
    try:
        with pytest.raises(crew.WatcherRequired):
            _dispatch(config_home, repo, "stale-live")
    finally:
        held.close()


def test_dispatch_admits_a_running_producer_whose_record_says_absent(
    isolated_project: tuple[Path, Path],
) -> None:
    """A producer that is running is admitted, although the record claims absence."""
    config_home, repo = isolated_project
    runner = _spawn_runner()
    try:
        _write_seat_record(
            "sample",
            {"pid": runner.pid, "pid_start_time": crew._process_start_time(runner.pid)},
        )
        record = _dispatch(config_home, repo, "live-desync")
        assert record["watch"]["watcher_live"] is True
        # The guard trusted the running process rather than re-arming one.
        assert record["watch"]["watcher"]["pid"] == runner.pid
    finally:
        runner.terminate()
        runner.wait(timeout=5)
