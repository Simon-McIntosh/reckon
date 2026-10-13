"""A launch whose path holds a leftover tree is released or refused by name.

A run's worktree path is named for its session and node, so every attempt to
launch one node in one session reaches for the same directory. A launch that
ends before writing a stream record can leave its tree behind, and every later
attempt then fails on the existing path, so the request cycles through the
queue and never launches. The launch now releases a tree a prior attempt left
when that tree is disposable — clean, with no commit beyond the launch base —
and refuses by name when it is not.

Every case works on a synthesised repository under a temporary config home; no
real fleet directory is read or written.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

routing_module = importlib.import_module("reckon.crew.routing")

PROJECT = "failed-launch-fixture"
SESSION = "failed-launch-session"
NODE = "review-of-the-thing"

# The seam a reader flips to treat every existing tree as not disposable: the
# negative control replaces this with a predicate that always returns False,
# after which the clean commit-free case fails on the existing path.
DISPOSABLE_SEAM = "_tree_is_disposable_for_launch"


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "."),
        ("commit", "-q", "-m", "test: seed the failed-launch fixture"),
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _repo_head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _target(repo: Path) -> Path:
    return routing_module._launch_worktree_path(repo, SESSION, NODE)


def _leftover_tree(repo: Path, revision: str = "HEAD") -> Path:
    tree = _target(repo)
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(tree), revision)
    return tree


def _registered(repo: Path) -> set[str]:
    return {
        str(Path(line.removeprefix("worktree ")).resolve())
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }


def _seed_ledger(repo: Path, run_id: str, base_sha: str) -> None:
    """A committed run whose ledger row names this node's tree path."""
    envelope = {
        "data": {
            "_version": 1,
            "members": [],
            "holds": [],
            "runs": [
                {
                    "run_id": run_id,
                    "project": PROJECT,
                    "session": SESSION,
                    "node": NODE,
                    "base_sha": base_sha,
                }
            ],
        }
    }
    (repo / "docs" / "state" / PROJECT / "crew.json").write_text(
        json.dumps(envelope), encoding="utf-8"
    )


def test_a_clean_commit_free_tree_is_released_and_the_launch_proceeds(
    repository: Path,
) -> None:
    """The headline case: a stale path does not block the next launch."""
    base = _repo_head(repository)
    old = _leftover_tree(repository, base)
    # The repository advances, so the leftover tree sits at its own older base,
    # which is an ancestor of the base the new launch is cut from.
    (repository / "advance.txt").write_text("advanced\n", encoding="utf-8")
    _git(repository, "add", "advance.txt")
    _git(repository, "commit", "-q", "-m", "test: advance the fixture head")
    advanced = _repo_head(repository)
    assert advanced != base

    payload = routing_module._create_worktree(repository, SESSION, NODE, "HEAD")

    assert payload.get("ok") is True, payload
    assert payload["path"] == str(old)
    assert old.is_dir(), "the launch left no worktree at the path"
    assert str(old) in _registered(repository)
    # The new tree is cut from the advanced head, which only a released leftover
    # tree allows.
    assert _git(old, "rev-parse", "HEAD").strip() == advanced


def test_a_tree_with_a_commit_beyond_its_base_is_kept_and_refused_by_name(
    repository: Path,
) -> None:
    base = _repo_head(repository)
    tree = _leftover_tree(repository, base)
    (tree / "work.txt").write_text("committed work\n", encoding="utf-8")
    _git(tree, "config", "user.email", "worker@example.invalid")
    _git(tree, "config", "user.name", "Worker")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-q", "-m", "test: a commit the attempt left behind")
    commit = _git(tree, "rev-parse", "HEAD").strip()
    assert commit != base
    attempt = "r-20261013T090000000000-review-of-the-thing"
    _seed_ledger(repository, attempt, base)

    with pytest.raises(routing_module.CrewError) as caught:
        routing_module._create_worktree(repository, SESSION, NODE, "HEAD")

    message = str(caught.value)
    assert attempt in message, message
    assert commit in message, message
    assert tree.is_dir() and (tree / "work.txt").is_file(), "the tree was removed"
    assert str(tree) in _registered(repository)


def test_a_tree_with_uncommitted_changes_is_kept_and_refused(
    repository: Path,
) -> None:
    base = _repo_head(repository)
    tree = _leftover_tree(repository, base)
    (tree / "untracked.txt").write_text("uncommitted\n", encoding="utf-8")
    attempt = "r-20261013T090000000001-review-of-the-thing"
    _seed_ledger(repository, attempt, base)

    with pytest.raises(routing_module.CrewError) as caught:
        routing_module._create_worktree(repository, SESSION, NODE, "HEAD")

    message = str(caught.value)
    assert attempt in message, message
    assert "uncommitted" in message, message
    assert tree.is_dir() and (tree / "untracked.txt").is_file(), "the tree was removed"
    assert str(tree) in _registered(repository)


def test_a_tree_with_a_live_pointer_is_kept_and_refused(
    repository: Path,
) -> None:
    base = _repo_head(repository)
    tree = _leftover_tree(repository, base)
    attempt = "r-20261013T090000000002-review-of-the-thing"
    _write_json(
        Path(pointer_path(attempt)),
        {
            "run_id": attempt,
            "pid": _dead_pid(),
            "phase": "dispatched",
            "worktree": str(tree),
            "repo": str(repository),
            "project": PROJECT,
            "node": NODE,
        },
    )
    run_dir(attempt).mkdir(parents=True, exist_ok=True)

    with pytest.raises(routing_module.CrewError) as caught:
        routing_module._create_worktree(repository, SESSION, NODE, "HEAD")

    message = str(caught.value)
    assert attempt in message, message
    assert tree.is_dir(), "the tree was removed despite a live pointer"
    assert str(tree) in _registered(repository)
