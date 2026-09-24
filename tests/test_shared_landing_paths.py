"""Dispatch grants for a plan's cumulative landing record."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs

dispatch = importlib.import_module("reckon.crew.dispatch")


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
def isolated_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Build the smallest mounted plan repository that dispatch can grant."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
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
    plan = repo / "docs" / "plans" / "scoring-should-stop-a-promotion.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="scoring-should-stop-a-promotion">
</head><body><h2 id="landing">Landing record</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", str(plan.relative_to(repo))],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    watcher = {"arming_line": "reckon crew watch --project sample", "pid": 7319}

    def watch_state(_project: str, *, session: str | None = None) -> dict:
        delivery = (
            runs.follower_state(_project, session) if session is not None else None
        )
        return {
            "arming_line": watcher["arming_line"],
            "watcher_live": bool(watcher.get("live")),
            "watcher": {"pid": watcher["pid"]} if watcher.get("live") else {},
            "attach_line": runs._watch_attach_line(_project, session=session),
            "session": session,
            "session_attached": None if delivery is None else bool(delivery["live"]),
            "follower": {} if delivery is None else delivery["follower"],
        }

    def ensure_watch(_project: str, *, session: str | None = None) -> dict:
        watcher["live"] = True
        return watch_state(_project, session=session)

    monkeypatch.setattr(dispatch, "watch_state", watch_state)
    monkeypatch.setattr(dispatch, "_ensure_watch_producer", ensure_watch)
    return config_home, repo


def _node(
    config_home: Path, name: str, *, write_paths: list[str] | None = None
) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="record one plan landing",
        plan="scoring-should-stop-a-promotion",
        section="landing",
        spec_level="guided",
        done_when="pytest reports one passing landing-path guard case",
        write_paths=write_paths or [f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _live_launcher(*_args, **_kwargs) -> int:
    """Stand in for the supervisor as a process that is running.

    The launcher seam replaces the supervisor, and the supervisor is a live
    process. A write claim is judged by the disposition of the run's recorded
    process, so a stub naming a pid that has exited leaves the owner's claim
    admitted past, and the exclusive-claim refusal under test never fires.
    """
    return os.getpid()


def _dispatch(
    config_home: Path,
    repo: Path,
    name: str,
    *,
    write_paths: list[str] | None = None,
) -> dict:
    session = f"session-{name}"
    with runs.follower_claim("sample", session, delivery="stream"):
        return crew.dispatch(
            node=_node(config_home, name, write_paths=write_paths),
            project="sample",
            repo=repo,
            config=CONFIG,
            session=session,
            launcher=_live_launcher,
        )


def test_shared_landing_paths_include_the_plan_figure_topic_directory(
    isolated_project: tuple[Path, Path],
) -> None:
    _config_home, repo = isolated_project
    node = _node(Path("manifest-root"), "topic")
    authority = {"plan": {"docs": str(repo / "docs"), "repository": str(repo)}}

    paths = dispatch._shared_landing_paths(node, project="sample", authority=authority)

    figure_topic = (
        repo / "docs" / "figures" / "scoring-should-stop-a-promotion"
    ).resolve()
    assert figure_topic in paths
    assert figure_topic.suffix == ""
    assert figure_topic == (repo / "docs" / "figures" / node.plan).resolve()


def test_dispatch_grants_the_plan_figure_topic_directory(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project

    record = _dispatch(config_home, repo, "figure-topic")

    assert (
        "docs/figures/scoring-should-stop-a-promotion" in record["node"]["write_paths"]
    )


def test_two_nodes_on_one_plan_share_its_figure_topic_directory(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project

    first = _dispatch(config_home, repo, "first")
    second = _dispatch(config_home, repo, "second")

    directory = "docs/figures/scoring-should-stop-a-promotion"
    assert directory in first["node"]["write_paths"]
    assert directory in second["node"]["write_paths"]


def test_two_nodes_cannot_claim_the_same_figure_filename(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    filename = "docs/figures/scoring-should-stop-a-promotion/landing.svg"
    owner = _dispatch(config_home, repo, "owner", write_paths=[filename])

    with pytest.raises(crew.ScopeConflict) as excinfo:
        _dispatch(config_home, repo, "second", write_paths=[filename])

    assert excinfo.value.run_id == owner["run_id"]
    assert excinfo.value.claimed_path == filename
