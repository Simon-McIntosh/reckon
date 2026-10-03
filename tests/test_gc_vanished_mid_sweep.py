"""gc carries past a worktree that vanishes between classification and action.

Measured 2026-10-03: ``crew gc --pin-unique-commits --apply`` stopped with
``[Errno 2] No such file or directory`` on a worktree a peer's promotion had
released. The directory was present when the pass classified the row as dirty
and still present when the action re-read it; it disappeared inside the pin
step, whose first act is ``git rev-parse HEAD`` run with the worktree as its
working directory. When a subprocess's working directory is gone, ``Popen``
raises ``FileNotFoundError`` before git starts, so the failure arrived as an
OSError rather than as a git error and the pass treated it as a defect of its
own and stopped. The pass now reports such a row as gone before action and
carries on to the remaining worktrees.

The earlier vanished-worktree coverage removes the directory before invoking
gc, so every read already sees the tree missing and the cheap unavailable
branch handles it; that test exercises no directory that disappears between
classification and action, and it releases only clean or merged trees, never
the pin path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.crew import routing
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
    (tree / "only-here.py").write_text(f"work that exists nowhere else: {name}\n")
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


def test_a_worktree_vanishing_before_its_pin_does_not_stop_the_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = repository(tmp_path)
    trees: dict[str, Path] = {}
    heads: dict[str, str] = {}
    for name in ("a-vanishing", "b-released", "c-released"):
        trees[name], heads[name] = divergent_dirty_worktree(root, name)

    real_step = routing._save_and_release_worktree
    acted: list[Path] = []

    def vanishing_step(repo, path, integrated_into, record=None, **kwargs):
        acted.append(Path(path))
        if len(acted) == 1:
            # The peer's release, landing between classification and this
            # action: the directory is gone by the time the step reads it.
            shutil.rmtree(path)
        return real_step(repo, path, integrated_into, record, **kwargs)

    monkeypatch.setattr(routing, "_save_and_release_worktree", vanishing_step)

    result = CliRunner().invoke(
        cli.main,
        ["crew", "gc", "--repo", str(root), "--pin-unique-commits", "--apply"],
    )

    # The sweep carries past the vanished tree rather than stopping on it.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True

    vanished = acted[0]
    assert not vanished.exists()
    rows = {Path(item["path"]).name: item for item in payload["worktrees"]}
    vanished_row = rows[vanished.name]

    # The vanished tree is reported as gone before action, never as removed.
    assert vanished_row["classification"] == "unavailable"
    assert vanished_row["gone_before_action"] is True
    assert "gone before action" in vanished_row["detail"]
    assert vanished_row["reclaimable"] is False
    assert "left in place" in vanished_row["withheld"]
    assert str(vanished) not in payload["removed_worktrees"]

    # Every remaining worktree still releases, its commits kept by the ref.
    released = [path for name, path in trees.items() if path != vanished]
    assert sorted(str(path) for path in released) == sorted(
        payload["removed_worktrees"]
    )
    for path in released:
        assert not path.exists()
        ref = f"{routing.ARCHIVE_REF_PREFIX}{path.name}"
        assert rows[path.name]["archive_ref"] == ref
        assert resolves(root, f"{ref}^{{commit}}") == heads[path.name]
        assert (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", heads[path.name], ref],
                cwd=root,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
