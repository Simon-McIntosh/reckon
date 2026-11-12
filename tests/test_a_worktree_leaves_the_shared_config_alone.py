"""Creating a worktree leaves the shared configuration alone when it can.

``extensions.worktreeConfig`` is a property of the shared checkout, not of the
worktree being created, so it reads ``true`` on every checkout that has
dispatched before. Writing it again on each creation contends for the shared
configuration lock with every concurrent dispatch: one holds the lock while the
other is refused outright, which is how a dispatched review failed with
``could not lock config file .git/config: File exists``.

So the setting is read first and written only when absent. When it must be
written and another git process holds the lock, the write is retried for a
bounded time and the lock is named if it is still held at the bound. The
worktree's own ``core.checkStat=minimal`` is set either way, in the worktree's
configuration rather than the shared one.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import routing as routing_module
from reckon.crew.node import CrewError

SESSION = "shared-config-session"
NODE_ID = "shared-config-node"
PROJECT = "shared-config-fixture"


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_config(path: Path, key: str, *scope: str) -> str | None:
    """A config key's value at ``path``, or ``None`` when it is unset."""
    result = subprocess.run(
        ["git", "config", *scope, "--get", key],
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
        ("commit", "-q", "-m", "chore: seed the shared-config fixture"),
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _shared_config(repo: Path) -> Path:
    return Path(_git(repo, "rev-parse", "--absolute-git-dir")) / "config"


def _snapshot(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns, stat.st_ino


def _hold_config_lock(repo: Path) -> Path:
    lock = _shared_config(repo).with_name("config.lock")
    lock.write_text("", encoding="utf-8")
    return lock


def _release_after(lock: Path, delay: float) -> threading.Timer:
    """Remove ``lock`` once ``delay`` seconds pass, from a background timer."""
    timer = threading.Timer(delay, lock.unlink)
    timer.start()
    return timer


def test_existing_setting_leaves_the_shared_config_alone(repository: Path) -> None:
    """With the setting already true, creation succeeds through a held lock."""
    _git(repository, "config", "extensions.worktreeConfig", "true")
    config = _shared_config(repository)
    # Let any timestamp land before the snapshot, so a write during creation
    # cannot be mistaken for a same-instant no-op.
    time.sleep(0.02)
    before = _snapshot(config)

    lock = _hold_config_lock(repository)
    try:
        routing_module._create_worktree(repository, SESSION, NODE_ID, "HEAD")
    finally:
        lock.unlink(missing_ok=True)

    after = _snapshot(config)
    assert after[0] == before[0], "the shared configuration's content changed"
    assert after[1] == before[1], "the shared configuration was rewritten"
    assert after[2] == before[2], "the shared configuration was replaced"

    tree = routing_module._launch_worktree_path(repository, SESSION, NODE_ID)
    assert _git_config(tree, "core.checkStat", "--worktree") == "minimal"


def test_absent_setting_is_written_once(repository: Path) -> None:
    """A repository without the setting gets it, and a second call adds nothing."""
    assert _git_config(repository, "extensions.worktreeConfig") is None

    routing_module._create_worktree(repository, SESSION, NODE_ID, "HEAD")

    assert _git_config(repository, "extensions.worktreeConfig") == "true"
    config = _shared_config(repository)
    time.sleep(0.02)
    settled = _snapshot(config)

    assert routing_module._enable_shared_worktree_config(repository) is False
    assert _snapshot(config) == settled


def test_absent_setting_waits_out_a_held_lock(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock released inside the bound is waited out, and creation succeeds."""
    monkeypatch.setattr(routing_module, "_WORKTREE_CONFIG_LOCK_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(routing_module, "_WORKTREE_CONFIG_LOCK_POLL_SECONDS", 0.02)
    assert _git_config(repository, "extensions.worktreeConfig") is None

    lock = _hold_config_lock(repository)
    timer = _release_after(lock, 0.25)
    try:
        routing_module._create_worktree(repository, SESSION, NODE_ID, "HEAD")
    finally:
        timer.join()
        lock.unlink(missing_ok=True)

    assert _git_config(repository, "extensions.worktreeConfig") == "true"
    tree = routing_module._launch_worktree_path(repository, SESSION, NODE_ID)
    assert _git_config(tree, "core.checkStat", "--worktree") == "minimal"


def test_held_lock_is_named_when_the_bound_expires(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock still held at the bound refuses, and the refusal names the lock."""
    monkeypatch.setattr(routing_module, "_WORKTREE_CONFIG_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(routing_module, "_WORKTREE_CONFIG_LOCK_POLL_SECONDS", 0.02)
    lock = _hold_config_lock(repository)
    try:
        with pytest.raises(CrewError) as refusal:
            routing_module._create_worktree(repository, SESSION, NODE_ID, "HEAD")
    finally:
        lock.unlink(missing_ok=True)

    message = str(refusal.value)
    assert "could not lock config file" in message
    assert str(lock) in message
    assert _git_config(repository, "extensions.worktreeConfig") is None
