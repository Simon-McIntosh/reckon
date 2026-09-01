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
from reckon.crew import recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS


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
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
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


def _dispatch(config_home: Path, repo: Path, name: str, **kwargs) -> dict:
    session = kwargs.pop("session", f"session-{name}")
    return crew.dispatch(
        node=_node(config_home, name),
        project="sample",
        repo=repo,
        config=CONFIG,
        session=session,
        launcher=lambda *args, **kwargs: os.getpid(),
        watch_required=True,
        **kwargs,
    )


def _attached(config_home: Path, repo: Path, name: str, **kwargs) -> dict:
    """Dispatch with this session's delivery registered, as a coordinator does."""
    session = kwargs.pop("session", f"session-{name}")
    with runs.follower_claim("sample", session, delivery="stream"):
        return _dispatch(config_home, repo, name, session=session, **kwargs)


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
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
                pool.submit(_attached, config_home, repo, "left"),
                pool.submit(_attached, config_home, repo, "right"),
            ]
            refusals = [
                future.exception(timeout=WATCHER_LOAD_BOUND_SECONDS)
                for future in futures
            ]
            records = [
                future.result(timeout=WATCHER_LOAD_BOUND_SECONDS) for future in futures
            ]

        watcher_pids = {record["watch"]["watcher"]["pid"] for record in records}
        assert refusals == [None, None]
        assert len(watcher_pids) == 1
        state = crew.project_watch_visibility("sample")
        assert state["watcher_live"] is True
        assert state["observer_alive"] is True
        assert state["watcher_required"] is True
    finally:
        recovery.unwatch("sample")
        _wait_for_stopped_producer()


def test_dispatch_restarts_a_producer_after_its_predecessor_dies(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    try:
        first = _attached(config_home, repo, "first")
        first_pid = first["watch"]["watcher"]["pid"]
        recovery.unwatch("sample")
        _wait_for_stopped_producer()

        second = _attached(config_home, repo, "second")

        assert second["watch"]["watcher_live"] is True
        assert second["watch"]["watcher"]["pid"] != first_pid
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()


def test_dispatch_refuses_a_session_whose_runs_would_finish_unheard(
    isolated_project: tuple[Path, Path],
) -> None:
    """A live seat is project-global; the wake-up it feeds is session-local.

    A guard satisfied by any producer — including one another session armed
    hours earlier — cannot fire for the case that actually goes silent: a
    coordinator dispatching with nothing consuming the ticker.
    """
    config_home, repo = isolated_project
    try:
        with pytest.raises(crew.WatcherRequired) as refusal:
            _dispatch(config_home, repo, "unheard", session="unattached")

        message = str(refusal.value)
        assert "unattached" in message
        assert runs._watch_attach_line("sample", session="unattached") in message
        assert not list(crew.list_live(project="sample")), "nothing may be created"
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()


def test_a_peer_sessions_follower_does_not_admit_this_dispatch(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    try:
        with runs.follower_claim("sample", "peer-session", delivery="stream"):
            with pytest.raises(crew.WatcherRequired):
                _dispatch(config_home, repo, "borrowed", session="mine")
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()


def test_a_waived_dispatch_records_what_it_waived(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    try:
        record = _dispatch(
            config_home, repo, "synchronous", session="sync", watch_override=True
        )
        assert record["watch_override"]["requested"] is True
        assert record["watch_override"]["session_attached"] is False
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()


def test_an_admitted_dispatch_records_its_session_attachment(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    try:
        record = _attached(config_home, repo, "attached")
        assert record["watch"]["session_attached"] is True
        assert record["watch"]["watcher_live"] is True
        assert record["watch"]["attach_line"] == runs._watch_attach_line(
            "sample", session="session-attached"
        )
    finally:
        recovery.unwatch("sample")
        _wait_for_stopped_producer()
