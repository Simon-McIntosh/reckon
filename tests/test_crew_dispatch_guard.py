"""Watcher dispatch guards over hermetic project state."""

from __future__ import annotations

import importlib
import json
import subprocess
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
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    watcher = {
        "arming_line": "reckon crew watch --project sample",
        "watcher_live": False,
        "watcher": {},
    }

    def watch_state(_project: str, *, session: str | None = None) -> dict:
        # The seat is faked; delivery is read from the real registry, because a
        # guard that both halves fake proves nothing about either.
        delivery = (
            runs.follower_state(_project, session) if session is not None else None
        )
        return {
            **watcher,
            "watcher": dict(watcher["watcher"]),
            "attach_line": runs._watch_attach_line(_project, session=session),
            "session": session,
            "session_attached": None if delivery is None else bool(delivery["live"]),
            "follower": {} if delivery is None else delivery["follower"],
        }

    def ensure_watch(_project: str, *, session: str | None = None) -> dict:
        watcher["watcher_live"] = True
        watcher["watcher"] = {"pid": 7319}
        return watch_state(_project, session=session)

    monkeypatch.setattr(dispatch_module, "watch_state", watch_state)
    monkeypatch.setattr(dispatch_module, "_ensure_watch_producer", ensure_watch)
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
    """Dispatch with this session's delivery registered, as a coordinator does.

    A producer alone does not admit a dispatch: the guard asks whether the
    dispatching session will hear the run finish, which is the only form of the
    question that a peer's seat cannot answer for you.
    """
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
            watch_override=watch_override,
        )


def test_first_dispatch_arms_a_watcher_without_a_waiver(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project

    record = _dispatch(config_home, repo, "first")

    assert record["watch"]["watcher_live"] is True
    assert record["watch"]["watcher"]["pid"] == 7319
    assert record["watch_override"] is None
    assert crew.read_pointer(record["run_id"])["watch_override"] is None


def test_occupied_project_reuses_the_watcher_armed_by_the_first_dispatch(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    owner = _dispatch(config_home, repo, "owner")

    accepted = _dispatch(config_home, repo, "accepted")

    assert accepted["watch"]["watcher_live"] is True
    assert accepted["watch"]["watcher"]["pid"] == owner["watch"]["watcher"]["pid"]
    assert crew.list_live(project="sample") == [owner, accepted]
    worktrees = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "node-accepted" in worktrees


def test_no_watch_override_is_recorded_for_an_occupied_project(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    _dispatch(config_home, repo, "owner")

    waived = _dispatch(config_home, repo, "waived", watch_override=True)

    assert waived["watch_override"] == {
        "requested": True,
        "arming_line": "reckon crew watch --project sample",
        "attach_line": (
            "reckon crew follow --project sample --session session-waived"
        ),
        "watcher_live": True,
        "session_attached": True,
    }
    assert crew.read_pointer(waived["run_id"])["watch_override"] == waived[
        "watch_override"
    ]


def test_occupied_project_with_a_live_watcher_accepts_another_dispatch(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    owner = _dispatch(config_home, repo, "owner")
    accepted = _dispatch(config_home, repo, "accepted")

    assert accepted["watch"]["watcher_live"] is True
    assert accepted["watch"]["watcher"]["pid"] == owner["watch"]["watcher"]["pid"]
    assert accepted["watch_override"] is None


def test_member_lookup_uses_project_mount_from_another_repository(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    config_home, plan_repo = isolated_project
    work_repo = tmp_path / "work-repo"
    scripts = work_repo / "skills" / "reckon-ship" / "scripts"
    scripts.mkdir(parents=True)
    (work_repo / "docs").mkdir()
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
    (work_repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(
            ["git", *arguments], cwd=work_repo, check=True, capture_output=True
        )
    mounts = json.loads((config_home / "mounts.json").read_text(encoding="utf-8"))
    mounts["work"] = str(work_repo / "docs")
    (config_home / "mounts.json").write_text(json.dumps(mounts), encoding="utf-8")
    crew.ledger.register_member("sample", "worker-a", harness="alpha", root=plan_repo)
    monkeypatch.chdir(work_repo)
    report = crew.reports_dir() / "sample" / "member-lookup.json"
    node = _node(config_home, "mounted-member")
    node.write_paths = [str(report)]

    record = crew.dispatch(
        node=node,
        project="sample",
        repo=Path.cwd(),
        config=CONFIG,
        session="mounted-member-session",
        member="worker-a",
        launcher=lambda *args, **kwargs: 4242,
    )

    assert record["member"] == "worker-a"
    assert record["repo"] == str(work_repo.resolve())
    assert record["authority"]["plan"]["repository"] == str(plan_repo.resolve())
    assert record["node"]["write_paths"] == [str(report)]
