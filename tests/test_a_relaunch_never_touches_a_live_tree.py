"""A dispatch of a run id with a live worker never rebuilds its tree.

A re-dispatch of a node on a non-local lane is not answered by the
existing-run branch, which is taken only for a queued request or on the lane
that queues. The dispatch therefore reaches worktree creation, and its own
claim overwrites the live pointer first, so the worker's tree reads as a
disposable leftover and is released and re-cut from a newer base. The worker
is left writing into a tree its fence no longer covers.

The check that stops this must run before anything of this dispatch is
written: before its claim, before its run directory, and before the worktree.
"""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import routing as routing_module
from reckon.crew import runs as runs_module

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

dispatch_module = importlib.import_module("reckon.crew.dispatch")

NODE_ID = "held-node"
SESSION = "dispatch-session"
RUN_ID = "r-held-run"


@pytest.fixture()
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKTREE_ROOT", str(tmp_path / "trees"))

    repo = tmp_path / "repo"
    (repo / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (repo / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "docs" / "plans" / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", ".gitignore", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(manifest_path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="record one dispatch guard result",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="pytest reports one passing guard case",
        write_paths=["src/candidate.py"],
        time_budget="20m",
        manifest_path=str(manifest_path),
    )


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _advance_head(repo: Path) -> str:
    """Commit on the repository so a rebuilt tree moves past the stand-in's head."""
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-q", "-m", "chore: later")
    return _git(repo, "rev-parse", "HEAD")


def _stand_in_tree(repo: Path) -> tuple[Path, str, Path]:
    """A worker's tree as a live run leaves it: clean by git, with its own file."""
    path = routing_module._launch_worktree_path(repo, SESSION, NODE_ID)
    routing_module._create_worktree(repo, SESSION, NODE_ID, "HEAD")
    note = path / "worker-note.log"
    note.write_text("in progress\n", encoding="utf-8")
    return path, _git(path, "rev-parse", "HEAD"), note


def _write_pointer(*, worktree: Path, pid: int, attempt: int) -> Path:
    record = {
        "run_id": RUN_ID,
        "project": "proj",
        "node": {"id": NODE_ID, "plan": "fixture", "section": "guard"},
        "phase": "working",
        "worktree": str(worktree),
        "pid": pid,
        "launcher_host": socket.gethostname(),
        "attempt": attempt,
    }
    path = runs_module.pointer_path(RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _live_stand_in() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dead_pid() -> int:
    finished = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(finished.stdout.strip())


def _dispatch(repo: Path, manifest: Path, *, launcher=lambda *a, **_: 4242) -> dict:
    return crew.dispatch(
        node=_node(manifest),
        project="proj",
        repo=repo,
        config=CONFIG,
        session=SESSION,
        run_id=RUN_ID,
        base="HEAD",
        launcher=launcher,
    )


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    if not directory.is_dir():
        return {}
    return {
        str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _assert_the_real_crew_home_is_untouched(tmp_path: Path) -> None:
    """The dispatch ran against the temporary home and wrote to no other."""
    assert tmp_path in runs_module.crew_home().parents
    real_home = Path.home() / ".config" / "reckon" / "crew"
    assert not (real_home / "live" / f"{RUN_ID}.json").exists()


def test_a_live_worker_refuses_the_dispatch_before_its_tree_is_touched(
    context: tuple[Path, Path], tmp_path: Path
) -> None:
    _config_home, repo = context
    tree, stand_in_head, note = _stand_in_tree(repo)
    process = _live_stand_in()
    try:
        pointer = _write_pointer(worktree=tree, pid=process.pid, attempt=2)
        pointer_before = pointer.read_text(encoding="utf-8")
        tree_after_base = _advance_head(repo)
        assert tree_after_base != stand_in_head
        records_before = _snapshot(runs_module.run_dir(RUN_ID))

        with pytest.raises(crew.CrewError) as refusal:
            _dispatch(repo, tmp_path / "manifests" / "held.md")

        assert "still has a live worker" in str(refusal.value)
        assert "cutting its worktree again" in str(refusal.value)
        assert RUN_ID in str(refusal.value)
        assert f"pid {process.pid}" in str(refusal.value)
        assert "attempt 2" in str(refusal.value)
        assert _git(tree, "rev-parse", "HEAD") == stand_in_head
        assert note.read_text(encoding="utf-8") == "in progress\n"
        assert _git(tree, "status", "--porcelain") == ""
        assert pointer.read_text(encoding="utf-8") == pointer_before
        assert _snapshot(runs_module.run_dir(RUN_ID)) == records_before
        _assert_the_real_crew_home_is_untouched(tmp_path)
    finally:
        process.terminate()
        process.wait()


def test_an_unreadable_pointer_refuses_rather_than_skipping_the_check(
    context: tuple[Path, Path], tmp_path: Path
) -> None:
    """A record that cannot be read says nothing, so silence is not permission."""
    _config_home, repo = context
    tree, stand_in_head, note = _stand_in_tree(repo)
    pointer = runs_module.pointer_path(RUN_ID)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("{ not a record\n", encoding="utf-8")
    _advance_head(repo)

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(repo, tmp_path / "manifests" / "corrupt.md")

    assert "cannot be read" in str(refusal.value)
    assert RUN_ID in str(refusal.value)
    assert "not valid JSON" in str(refusal.value)
    assert _git(tree, "rev-parse", "HEAD") == stand_in_head
    assert note.read_text(encoding="utf-8") == "in progress\n"
    assert _git(tree, "status", "--porcelain") == ""
    assert pointer.read_text(encoding="utf-8") == "{ not a record\n"
    assert not runs_module.run_dir(RUN_ID).exists()
    _assert_the_real_crew_home_is_untouched(tmp_path)


def test_a_worker_proven_dead_lets_the_dispatch_proceed(
    context: tuple[Path, Path], tmp_path: Path
) -> None:
    _config_home, repo = context
    tree, _stand_in_head, note = _stand_in_tree(repo)
    _write_pointer(worktree=tree, pid=_dead_pid(), attempt=2)
    tree_after_base = _advance_head(repo)

    record = _dispatch(repo, tmp_path / "manifests" / "dead.md")

    assert record["run_id"] == RUN_ID
    assert _git(tree, "rev-parse", "HEAD") == tree_after_base
    assert not note.exists()
    _assert_the_real_crew_home_is_untouched(tmp_path)


def test_the_refusal_is_raised_before_the_worktree_is_created(
    context: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_home, repo = context
    tree, _stand_in_head, _note = _stand_in_tree(repo)
    process = _live_stand_in()
    calls: list[tuple] = []

    def spy(*arguments, **keywords):
        calls.append((arguments, keywords))
        raise AssertionError("the worktree was created over a live worker")

    monkeypatch.setattr(dispatch_module, "_create_worktree", spy)
    try:
        _write_pointer(worktree=tree, pid=process.pid, attempt=2)
        _advance_head(repo)

        with pytest.raises(crew.CrewError) as refusal:
            _dispatch(repo, tmp_path / "manifests" / "spy.md")

        assert "still has a live worker" in str(refusal.value)
        assert calls == []
    finally:
        process.terminate()
        process.wait()
