"""Unnamed review dispatches do not contend through a coordinator member."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
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
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="reviews">Independent review dispatch</h2>',
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

    watch = {
        "watcher_live": True,
        "session_attached": True,
        "watcher": {"pid": os.getpid()},
        "arming_line": "watch",
        "attach_line": "follow",
    }
    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    monkeypatch.setattr(
        dispatch_module, "_ensure_watch_producer", lambda *args, **kwargs: dict(watch)
    )
    monkeypatch.setattr(
        dispatch_module, "watch_state", lambda *args, **kwargs: dict(watch)
    )
    return config_home, repo


def _scoring_pointer(config_home: Path, repo: Path, run_id: str) -> dict:
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: {run_id}\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "reviews"},
        "backend": "worker",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def test_two_unnamed_reviews_launch_with_disposable_members(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    first_source = _scoring_pointer(config_home, repo, "r-first")
    second_source = _scoring_pointer(config_home, repo, "r-second")
    roster_path = ledger.ledger_path("sample", repo)
    roster_before = roster_path.read_bytes() if roster_path.is_file() else None
    launched: list[dict] = []

    def launcher(*args, **kwargs):
        launched.append(kwargs)
        return os.getpid()

    first = recovery.dispatch_review_for_run(
        first_source, config=CONFIG, launcher=launcher
    )
    second = recovery.dispatch_review_for_run(
        second_source, config=CONFIG, launcher=launcher
    )

    assert first["dispatched"] is True
    if not second["dispatched"]:
        pytest.fail(f"second review refusal: {second.get('reason', second)}")
    assert second["dispatched"] is True, second
    assert len(launched) == 2
    first_pointer = crew.read_pointer(first["review_run_id"])
    second_pointer = crew.read_pointer(second["review_run_id"])
    assert first_pointer["member"] != second_pointer["member"]
    assert first_pointer["session_id"] is None
    assert second_pointer["session_id"] is None
    assert roster_path.is_file() is (roster_before is not None)
    if roster_before is not None:
        assert roster_path.read_bytes() == roster_before


def test_explicit_member_still_refuses_a_live_second_dispatch(
    isolated_project: tuple[Path, Path],
) -> None:
    _config_home, repo = isolated_project
    node = crew.TaskNode(
        id="first",
        goal="launch one explicit member",
        plan="fixture",
        section="reviews",
        role="review",
        spec_level="exact",
        done_when="pytest tests/test_no_member_dispatch_runs_alone.py reports the launch",
        write_paths=["records/first.json"],
        time_budget="20m",
    )
    ledger.register_member("sample", "named-member", harness="worker", root=repo)
    first = crew.dispatch(
        node=node,
        project="sample",
        repo=repo,
        config=CONFIG,
        session="session-orchestrating",
        member="named-member",
        launcher=lambda *args, **kwargs: os.getpid(),
    )

    with pytest.raises(crew.MemberInFlight):
        crew.dispatch(
            node=node,
            project="sample",
            repo=repo,
            config=CONFIG,
            session="session-orchestrating",
            member="named-member",
            launcher=lambda *args, **kwargs: pytest.fail(
                "the explicit member must remain serialised"
            ),
        )

    assert first["member"] == "named-member"
