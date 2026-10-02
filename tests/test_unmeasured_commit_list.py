from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from reckon.crew.routing import _inspect_workspace, _workspace_roots


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


def divergent_dirty_worktree(root: Path) -> tuple[Path, str]:
    """A worktree carrying a commit off the integration head, plus residue."""
    tree = worktree(root, "divergent")
    (tree / "only-here.py").write_text("work that exists nowhere else\n")
    git(tree, "add", "only-here.py")
    git(tree, "commit", "-q", "-m", "test: divergent commit")
    head = git(tree, "rev-parse", "HEAD")
    (tree / "notes.txt").write_text("residue\n")
    return tree, head


def test_a_divergent_dirty_worktree_reads_null_unmeasured_and_a_list_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One worktree inspected both ways.

    An empty list from a row whose commits were never compared reads as a
    measured absence: a caller cannot tell "no commits beyond the integration
    head" from "this caller did not ask". So the unmeasured row carries nulls
    and names why, and the measured row carries the lists.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    tree, head = divergent_dirty_worktree(root)

    unmeasured = _inspect_workspace(root, tree, "HEAD", ())
    assert unmeasured["non_equivalent_commits"] is None
    assert unmeasured["patch_equivalent_commits"] is None
    assert "release_residue" in unmeasured["commits_unmeasured_reason"]

    measured = _inspect_workspace(root, tree, "HEAD", (), release_residue=True)
    assert [commit["sha"] for commit in measured["non_equivalent_commits"]] == [head]
    assert measured["patch_equivalent_commits"] == []
    assert measured["commits_unmeasured_reason"] == ""


def test_a_reachable_head_names_the_ancestor_as_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    tree = worktree(root, "reachable")
    (tree / "notes.txt").write_text("residue\n")

    row = _inspect_workspace(root, tree, "HEAD", (), release_residue=True)

    assert row["non_equivalent_commits"] is None
    assert row["patch_equivalent_commits"] is None
    assert "ancestor" in row["commits_unmeasured_reason"]


def test_an_unavailable_tree_reads_null_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    tree = worktree(root, "vanished")
    shutil.rmtree(tree)

    row = _inspect_workspace(root, tree, "HEAD", (), raise_on_unavailable=False)

    assert row["classification"] == "unavailable"
    assert row["non_equivalent_commits"] is None
    assert row["patch_equivalent_commits"] is None
    assert "unavailable" in row["commits_unmeasured_reason"]
