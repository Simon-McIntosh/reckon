"""crew gc's --pin-unique-commits reaches the sweep's pin option.

The sweep can pin a dirty worktree's commits that exist nowhere else to an
archive ref before releasing the tree, but the command line exposed no flag
for it. These tests drive the flag through the CLI over a temporary
repository holding one dirty, unintegrated worktree, then read the archive
ref and the worktree back off disk: with the flag and --apply the ref
resolves and the tree is gone; without the flag the tree stays.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.crew.routing import _workspace_roots


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


def divergent_dirty_worktree(root: Path, name: str) -> tuple[Path, str]:
    """A dirty worktree carrying one commit that exists nowhere else."""
    tree = _workspace_roots(root)[0] / "session" / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    (tree / "only-here.py").write_text("work that exists nowhere else\n")
    git(tree, "add", "only-here.py")
    git(tree, "commit", "-q", "-m", "test: work that exists nowhere else")
    head = git(tree, "rev-parse", "HEAD")
    (tree / "notes.txt").write_text("residue\n")
    return tree, head


def resolves(root: Path, revision: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", revision],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def run_gc(root: Path, *flags: str):
    result = CliRunner().invoke(cli.main, ["crew", "gc", "--repo", str(root), *flags])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_the_flag_pins_the_commits_then_the_worktree_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    tree, head = divergent_dirty_worktree(root, "pinned")
    ref = f"refs/reckon/archive/{tree.name}"

    # The flag needs --apply to act: the dry run only reports the row as
    # reclaimable and leaves both the tree and the ref alone.
    dry = run_gc(root, "--pin-unique-commits")
    dry_row = next(item for item in dry["worktrees"] if item["path"] == str(tree))
    assert dry["dry_run"] is True
    assert dry_row["reclaimable"] is True
    assert dry["removed_worktrees"] == []
    assert resolves(root, ref) is None
    assert tree.exists()

    report = run_gc(root, "--pin-unique-commits", "--apply")
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["archive_ref"] == ref
    assert row["head"] == head
    assert str(tree) in report["removed_worktrees"]
    assert not tree.exists()
    assert resolves(root, f"{ref}^{{commit}}") == head
    assert git(root, "show", f"{ref}:only-here.py") == "work that exists nowhere else"


def test_without_the_flag_the_worktree_stays(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    tree, _ = divergent_dirty_worktree(root, "kept")
    ref = f"refs/reckon/archive/{tree.name}"

    report = run_gc(root, "--apply")
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["classification"] == "dirty"
    assert row["reclaimable"] is False
    assert str(tree) not in report["removed_worktrees"]
    assert resolves(root, ref) is None
    assert tree.exists()
