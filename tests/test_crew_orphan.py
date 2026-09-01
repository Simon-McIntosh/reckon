"""Hermetic observer-liveness contracts for project watchers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs

crew_dispatch = import_module("reckon.crew.dispatch")


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
def home(tmp_path, monkeypatch):
    """Move every watcher and run pointer into temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path, home):
    """Build the smallest committed repository a refused dispatch can inspect."""
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-ship" / "scripts"
    plans = root / "docs" / "plans"
    scripts.mkdir(parents=True)
    plans.mkdir(parents=True)
    source_root = Path(__file__).parents[1]
    (scripts / "worktree_fleet.py").write_text(
        (
            source_root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py"
        ).read_text()
    )
    (plans / "delivery.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="delivery">
</head><body><h2 id="guard">Guard</h2></body></html>
"""
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/delivery.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


@pytest.fixture()
def orphan_processes():
    """Stop only exact orphan watcher processes started by the current test."""
    owned: list[tuple[int, str | None]] = []
    yield owned
    for pid, start_time in owned:
        if crew._process_start_time(pid) != start_time:
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.is_file():
        if time.monotonic() >= deadline:
            pytest.fail(f"watcher did not register at {path}")
        time.sleep(0.01)


def _spawn_orphan_watcher(
    project: str, tmp_path: Path, owned: list[tuple[int, str | None]]
) -> tuple[int, dict]:
    source_root = Path(__file__).parents[1]
    ready = tmp_path / "watcher-ready.json"
    watcher_script = """
import json
import sys
import time
from pathlib import Path
from reckon.crew.recovery import _watch_registration

project, ready = sys.argv[1:]
with _watch_registration(project, "1h") as (acquired, record):
    Path(ready).write_text(json.dumps({"acquired": acquired, "record": record}))
    if acquired:
        time.sleep(30)
"""
    launcher_script = """
import subprocess
import sys

watcher_script, project, ready = sys.argv[1:]
child = subprocess.Popen(
    [sys.executable, "-c", watcher_script, project, ready],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
print(child.pid, flush=True)
"""
    launcher = subprocess.run(
        [
            sys.executable,
            "-c",
            launcher_script,
            watcher_script,
            project,
            str(ready),
        ],
        cwd=source_root,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        check=True,
        capture_output=True,
        text=True,
    )
    pid = int(launcher.stdout.strip())
    _wait_for_file(ready)
    owned.append((pid, crew._process_start_time(pid)))
    registered = json.loads(ready.read_text())
    assert registered["acquired"] is True
    return pid, registered["record"]


def _node(home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="next-node",
        goal="record observer liveness for one watcher",
        plan="delivery",
        section="guard",
        done_when="pytest tests/test_crew_orphan.py passes",
        write_paths=["reckon/next.py"],
        time_budget="20m",
        manifest_path=str(home / "manifest.md"),
    )


def test_registration_records_a_living_parent_and_remains_live(home) -> None:
    parent_pid = os.getppid()

    with recovery._watch_registration("proj", "1h") as (acquired, watcher):
        assert acquired is True
        assert watcher["parent_pid"] == parent_pid
        assert watcher["parent_start_time"] == crew._process_start_time(parent_pid)
        assert json.loads(crew.watch_lock_path("proj").read_text()) == watcher

        state = crew.project_watch_visibility("proj")
        assert state["watcher_live"] is True
        assert state["observer_alive"] is True
        assert state["pid"] == watcher["pid"]


def test_an_orphaned_watcher_is_replaced_rather_than_refused(
    home, repo, tmp_path, orphan_processes
) -> None:
    """A producer nobody supervises is swapped out, not argued with.

    It keeps appending real transitions, so refusing a dispatch over it would
    block work on account of a producer that is streaming perfectly. But nothing
    will ever replace it and it holds the seat lock, so accepting it silently
    lets a stale seat outlive every session that cared — measured at four days in
    one project. Arming therefore replaces it.

    This test used to substitute the orphan-aware `watch_state` into the dispatch
    module and then assert the refusal it produced, so it asserted a property of
    a function dispatch never calls: it passed green while production admitted.
    Nothing is patched here.
    """
    pid, watcher = _spawn_orphan_watcher("proj", tmp_path, orphan_processes)
    assert watcher["pid"] == pid

    visibility = crew.project_watch_visibility("proj")
    assert visibility["seat_held"] is True
    assert visibility["observer_alive"] is False, "its supervisor is gone"

    # A real coordinator has its own delivering follower; `watch_override` would
    # skip the arming path this test exists to exercise.
    with runs.follower_claim("proj", "session", delivery="stream"):
        record = crew_dispatch.dispatch(
            node=_node(home),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="session",
            launcher=lambda *_args, **_kwargs: 4242,
            watch_required=True,
        )

    replacement = crew.project_watch_visibility("proj")
    assert replacement["seat_held"] is True
    assert replacement["pid"] != pid, "the orphan was replaced, not reused"
    assert replacement["observer_alive"] is True
    assert record["watch"]["watcher_live"] is True

    stopped = recovery.unwatch("proj")
    assert stopped["stopped"] is True
    assert stopped["registration_released"] is True
    assert json.loads(crew.watch_lock_path("proj").read_text()) == {}
