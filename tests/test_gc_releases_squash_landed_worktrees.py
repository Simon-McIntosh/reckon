from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from reckon.crew.routing import _workspace_roots, garbage_collect


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "source.py").write_text("original\n")
    git(root, "add", "source.py")
    git(root, "commit", "-q", "-m", "test: seed")
    return root


def worktree(root: Path, name: str, revision: str = "HEAD") -> Path:
    tree = _workspace_roots(root)[0] / "session" / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-q", "--detach", str(tree), revision)
    return tree


def landed_elsewhere(root: Path, tree: Path, message: str) -> str:
    """Commit a change in the worktree, then land the same patch on main.

    The cherry-pick reproduces the worktree commit's patch under a new commit,
    which is how a squash or a rebase lands a branch's content without its
    history: the worktree head is not an ancestor of main, yet its change is.
    The landing commit carries its own message so its sha differs even when the
    patch and the parent are identical.
    """
    (tree / "source.py").write_text(message + "\n")
    git(tree, "add", "source.py")
    git(tree, "commit", "-q", "-m", message)
    head = git(tree, "rev-parse", "HEAD")
    git(root, "cherry-pick", "--no-commit", head)
    git(root, "commit", "-q", "-m", "test: land " + message)
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", head, "HEAD"],
            cwd=root,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    ), "the worktree head must not be an ancestor, or the case is not exercised"
    return head


def test_a_squash_landed_dirty_tree_releases_with_its_residue_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = repository(tmp_path)
    tree = worktree(root, "squashed")
    head = landed_elsewhere(root, tree, "test: work that landed as a squash")
    (tree / "notes.txt").write_text("residue\n")

    report = garbage_collect(repo=root, apply=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["classification"] == "dirty-integrated"
    assert row["head"] == head
    assert [commit["sha"] for commit in row["patch_equivalent_commits"]] == [head]
    assert row["non_equivalent_commits"] == []
    assert str(tree) in report["removed_worktrees"]
    assert not tree.exists()
    archive = Path(row["residue_tar"])
    with tarfile.open(archive) as saved:
        assert saved.extractfile("notes.txt").read() == b"residue\n"
    assert row["residue_classes"]["notes.txt"] == "unique"


def test_a_tree_whose_commit_landed_nowhere_is_kept_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = repository(tmp_path)
    tree = worktree(root, "unlanded")
    (tree / "only-here.py").write_text("work that exists nowhere else\n")
    git(tree, "add", "only-here.py")
    git(tree, "commit", "-q", "-m", "test: work that landed nowhere")
    head = git(tree, "rev-parse", "HEAD")
    (tree / "notes.txt").write_text("residue\n")

    report = garbage_collect(repo=root, apply=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["classification"] == "dirty"
    assert row["reclaimable"] is False
    assert row["non_equivalent_commits"] == [
        {"sha": head, "subject": "test: work that landed nowhere", "equivalent": False}
    ]
    assert str(tree) not in report["removed_worktrees"]
    assert tree.exists()
    assert (tree / "only-here.py").is_file()


def test_a_live_claimed_squash_landed_tree_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = repository(tmp_path)
    tree = worktree(root, "claimed")
    landed_elsewhere(root, tree, "test: work that landed as a squash")
    (tree / "notes.txt").write_text("residue\n")
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    (live / "run-live.json").write_text(
        json.dumps(
            {
                "run_id": "run-live",
                "worktree": str(tree),
                "phase": "working",
                "pid": 999999999,
            }
        )
    )

    report = garbage_collect(repo=root, apply=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["classification"] == "live-referenced"
    assert row["claimed_by_live_runs"] == ["run-live"]
    assert str(tree) not in report["removed_worktrees"]
    assert tree.exists()
    assert (tree / "notes.txt").read_text() == "residue\n"
