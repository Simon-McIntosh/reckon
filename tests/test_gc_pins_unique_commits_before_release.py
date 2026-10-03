"""gc releases a worktree with unique commits after pinning them to a ref.

A dirty worktree carrying commits with no patch-equivalent on the integration
head is kept by default, because removing it would destroy the only copy of
that work. When the pin is explicitly requested, gc first points
``refs/reckon/archive/<worktree name>`` at the worktree head, records that ref
in the residue directory beside the patch, and only then releases the tree
through the residue-preserving path, so the commits stay reachable after the
directory is gone. A sweep that stops mid-pass names the worktrees it removed
and those it refused separately, and a defect in the sweep's own code surfaces
as its traceback rather than as an ordinary refusal.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import routing
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


def divergent_dirty_worktree(root: Path, name: str) -> tuple[Path, str]:
    """A dirty worktree carrying one commit that exists nowhere else."""
    tree = worktree(root, name)
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


def test_a_unique_commit_tree_releases_after_its_commits_are_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin keeps the commits resolvable after the worktree is gone."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    tree, head = divergent_dirty_worktree(root, "pinned")
    ref = f"refs/reckon/archive/{tree.name}"

    report = garbage_collect(repo=root, apply=True, pin_unique_commits=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["reclaimable"] is True
    assert row["archive_ref"] == ref
    assert row["head"] == head
    assert str(tree) in report["removed_worktrees"]
    assert not tree.exists()

    # The archived commits still resolve through the ref.
    assert resolves(root, f"{ref}^{{commit}}") == head
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", head, ref],
            cwd=root,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    assert git(root, "show", f"{ref}:only-here.py") == "work that exists nowhere else"

    # The ref is recorded in the residue directory beside the patch.
    saved = json.loads(Path(row["residue_classification"]).read_text())
    assert saved["archive_ref"] == ref
    assert Path(row["residue_patch"]).is_file()
    assert Path(row["residue_tar"]).is_file()


def test_a_unique_commit_tree_without_the_option_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the option nothing changes: the tree stays, and no ref appears."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    tree, _ = divergent_dirty_worktree(root, "kept")
    ref = f"refs/reckon/archive/{tree.name}"

    report = garbage_collect(repo=root, apply=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["classification"] == "dirty"
    assert row["reclaimable"] is False
    assert resolves(root, ref) is None
    assert str(tree) not in report["removed_worktrees"]
    assert tree.exists()
    assert (tree / "only-here.py").read_text() == "work that exists nowhere else\n"


def test_an_occupied_archive_ref_is_refused_and_the_earlier_pin_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two worktrees can share a directory name; the earlier pin is not lost."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    tree, _ = divergent_dirty_worktree(root, "pinned")
    ref = f"refs/reckon/archive/{tree.name}"
    earlier = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", ref, earlier)

    with pytest.raises(routing.GcSweepError) as caught:
        garbage_collect(repo=root, apply=True, pin_unique_commits=True)

    assert ref in str(caught.value)
    assert caught.value.partial["refused_worktrees"] == [str(tree)]
    assert resolves(root, ref) == earlier
    assert tree.exists()


def test_a_stopped_sweep_carries_a_refused_list_beside_the_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree whose removal was attempted and failed is not in the removed list."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    released = worktree(root, "a-released")
    locked = worktree(root, "z-locked")
    git(root, "worktree", "lock", str(locked))

    with pytest.raises(routing.GcSweepError) as caught:
        garbage_collect(repo=root, apply=True)

    partial = caught.value.partial
    assert partial["removed_worktrees"] == [str(released)]
    assert partial["refused_worktrees"] == [str(locked)]
    assert partial["failed_path"] == str(locked)
    assert not released.exists()
    assert locked.exists()


@pytest.mark.parametrize("error", [TypeError, AttributeError, NameError])
def test_a_programming_error_inside_the_sweep_surfaces_as_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """A defect in gc's own code is a traceback, not a refusal of a tree."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)

    def explode(_repo: Path) -> list[Path]:
        raise error("a defect in the sweep's own code")

    monkeypatch.setattr(routing, "_workspace_roots", explode)

    with pytest.raises(error, match="a defect in the sweep's own code"):
        routing.garbage_collect(repo=root, apply=True)
