"""A worktree reckons creates compares file stat the way the fence allows.

A fenced worker's user namespace maps only the caller's own uid and primary
gid, so the index it writes records the overflow gid for every entry against
files that carry the supplementary group. Under git's default stat comparison
every git call outside the fence then reads every entry as changed and re-reads
every file — 270 s of a 621 s no-code promotion on one measured review.

So a worktree is created with ``core.checkStat=minimal`` in its own
configuration, reached through ``extensions.worktreeConfig`` on the shared
configuration, and the main checkout keeps the default comparison.

The control shows the narrowed comparison still catches an edit: a tracked file
is rewritten to the same size with its mtime restored, so the whole-second
ctime is the only field that moved.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon.crew import routing as routing_module

SESSION = "fence-index-session"
NODE_ID = "fence-index-node"
PROJECT = "fence-index-fixture"


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_config(path: Path, key: str) -> str | None:
    """A config key's value at ``path``, or ``None`` when it is unset."""
    result = subprocess.run(
        ["git", "config", "--get", key],
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout with one tracked file, and no real fleet state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKTREE_ROOT", str(tmp_path / "trees"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "chore: seed the fence-index fixture"),
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def test_created_worktree_compares_minimal_and_main_keeps_default(
    repository: Path,
) -> None:
    tree = routing_module._launch_worktree_path(repository, SESSION, NODE_ID)
    routing_module._create_worktree(repository, SESSION, NODE_ID, "HEAD")

    assert tree.is_dir(), "the fleet script creates the tree at the launch path"
    assert _git_config(tree, "core.checkStat") == "minimal"
    # The extension is the shared switch that lets a worktree carry its own
    # configuration; the main checkout must still read the default comparison.
    assert _git_config(repository, "extensions.worktreeConfig") == "true"
    assert _git_config(repository, "core.checkStat") is None


def test_minimal_comparison_still_catches_an_edit_by_its_ctime(
    repository: Path,
) -> None:
    tree = routing_module._launch_worktree_path(repository, SESSION, NODE_ID)
    routing_module._create_worktree(repository, SESSION, NODE_ID, "HEAD")

    tracked = tree / "seed.txt"
    before = tracked.stat()
    index = Path(_git(tree, "rev-parse", "--absolute-git-dir")) / "index"
    # The whole-second clock must move past the index write, or a same-second
    # ctime would be indistinguishable and the control would prove nothing.
    index_second = int(index.stat().st_mtime)
    while int(time.time()) <= index_second:
        time.sleep(0.05)

    # Same size, restored mtime: uid, gid, device, inode and size are all as the
    # index recorded them, so only the whole-second ctime can name this change.
    tracked.write_text("SEED\n", encoding="utf-8")
    os.utime(tracked, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = tracked.stat()

    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ctime_ns != before.st_ctime_ns
    assert int(after.st_ctime_ns / 1_000_000_000) > int(
        before.st_ctime_ns / 1_000_000_000
    ), "the control depends on the ctime moving at whole-second resolution"

    assert _git(tree, "diff", "--name-only") == "seed.txt"
