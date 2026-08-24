"""Watcher dispatch guards over hermetic project state."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from reckon import crew


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
        subprocess.run(
            ["git", *arguments], cwd=repo, check=True, capture_output=True
        )
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


def _dispatch(
    config_home: Path,
    repo: Path,
    name: str,
    *,
    watch_override: bool = False,
) -> dict:
    return crew.dispatch(
        node=_node(config_home, name),
        project="sample",
        repo=repo,
        config=CONFIG,
        session=f"session-{name}",
        launcher=lambda *args, **kwargs: 4242,
        watch_required=True,
        watch_override=watch_override,
    )


def _deliver(record: dict) -> None:
    manifest = Path(record["manifest_path"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: watcher-owner\nstatus: complete\ncommits: HEAD\n",
        encoding="utf-8",
    )


def test_first_dispatch_does_not_require_a_watcher_or_waiver(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project

    record = _dispatch(config_home, repo, "first")

    assert record["watch"]["watcher_live"] is False
    assert record["watch_override"] is None
    assert crew.read_pointer(record["run_id"])["watch_override"] is None


def test_occupied_project_without_a_watcher_refuses_before_worktree_creation(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    owner = _dispatch(config_home, repo, "owner")

    with pytest.raises(crew.WatcherRequired) as excinfo:
        _dispatch(config_home, repo, "refused")

    assert excinfo.value.watch["watcher_live"] is False
    assert excinfo.value.watch["arming_line"] == (
        "reckon crew watch --project sample"
    )
    assert excinfo.value.watch["arming_line"] in str(excinfo.value)
    assert crew.list_live(project="sample") == [owner]
    worktrees = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "node-refused" not in worktrees


def test_no_watch_override_is_recorded_for_an_occupied_project(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    _dispatch(config_home, repo, "owner")

    waived = _dispatch(config_home, repo, "waived", watch_override=True)

    assert waived["watch_override"] == {
        "requested": True,
        "arming_line": "reckon crew watch --project sample",
        "watcher_live": False,
    }
    assert crew.read_pointer(waived["run_id"])["watch_override"] == waived[
        "watch_override"
    ]


def test_occupied_project_with_a_live_watcher_accepts_another_dispatch(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    owner = _dispatch(config_home, repo, "owner")
    sleeping = threading.Event()
    release = threading.Event()

    def controlled_sleep(_seconds: float) -> None:
        sleeping.set()
        assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        watcher = pool.submit(
            crew.watch,
            "sample",
            stall_window="1h",
            sleeper=controlled_sleep,
        )
        assert sleeping.wait(timeout=5)

        accepted = _dispatch(config_home, repo, "accepted")

        assert accepted["watch"]["watcher_live"] is True
        assert accepted["watch"]["watcher"]["pid"] == os.getpid()
        assert accepted["watch_override"] is None

        _deliver(owner)
        release.set()
        event = watcher.result(timeout=5)
        assert event["event"] == "terminal"
        assert event["run_id"] == owner["run_id"]
