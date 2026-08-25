"""Dispatch-owned lifecycle for the project watch producer."""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery


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
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-ship" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="arming">Producer arming</h2>',
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
        subprocess.run(
            ["git", *arguments], cwd=repo, check=True, capture_output=True
        )
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="record one producer-backed dispatch",
        plan="fixture",
        section="arming",
        done_when="pytest reports one live watcher for every dispatch",
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _dispatch(config_home: Path, repo: Path, name: str) -> dict:
    return crew.dispatch(
        node=_node(config_home, name),
        project="sample",
        repo=repo,
        config=CONFIG,
        session=f"session-{name}",
        launcher=lambda *args, **kwargs: os.getpid(),
        watch_required=True,
    )


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not crew.watch_state("sample")["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def test_concurrent_dispatches_arm_exactly_one_detached_producer(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_dispatch, config_home, repo, "left"),
                pool.submit(_dispatch, config_home, repo, "right"),
            ]
            refusals = [future.exception(timeout=10) for future in futures]
            records = [future.result(timeout=10) for future in futures]

        watcher_pids = {record["watch"]["watcher"]["pid"] for record in records}
        assert refusals == [None, None]
        assert len(watcher_pids) == 1
        state = recovery.watch_state("sample")
        assert state["watcher_live"] is True
        assert state["watcher"]["parent_pid"] != os.getpid()
        assert state["watcher"]["parent_pid"] > 1
    finally:
        recovery.unwatch("sample")
        _wait_for_stopped_producer()


def test_dispatch_restarts_a_producer_after_its_predecessor_dies(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    try:
        first = _dispatch(config_home, repo, "first")
        first_pid = first["watch"]["watcher"]["pid"]
        recovery.unwatch("sample")
        _wait_for_stopped_producer()

        second = _dispatch(config_home, repo, "second")

        assert second["watch"]["watcher_live"] is True
        assert second["watch"]["watcher"]["pid"] != first_pid
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()
