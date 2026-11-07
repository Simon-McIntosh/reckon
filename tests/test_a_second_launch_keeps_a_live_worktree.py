"""A second launch of a run keeps the tree its first launch holds.

A worktree path is named for the session and node, not for the run, so every
launch of that node reaches the same directory. A duplicate launch therefore
meets the tree of the launch already running. The launch side reads its own
run's pointer as a claim, so the duplicate refuses and names the live run
rather than moving the running worker's tree into ``_trash``.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from reckon.crew import routing, runs
from reckon.crew.node import CrewError
from tests.test_a_queued_launch_keeps_the_run_record import (
    SESSION,
)
from tests.test_a_queued_launch_keeps_the_run_record import (
    admit_repo as _admit_repo,
)

admit_repo = _admit_repo

NODE = "sample"
RUN = "r-live-launch"
BASE = "HEAD"


@pytest.fixture()
def live_pid() -> int:
    process = subprocess.Popen(["sleep", "60"])
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait(timeout=10)


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait(timeout=10)
    return process.pid


def _launch_path(repository: Path) -> Path:
    return routing._launch_worktree_path(repository, SESSION, NODE)


def _tree_inode(path: Path) -> int:
    return path.stat().st_ino


def _publish(repository: Path, phase: str, tree: Path, *, pid: int | None) -> None:
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "session": SESSION,
            "phase": phase,
            "worktree": str(tree),
            "launcher_host": socket.gethostname(),
            "pid": pid,
        },
    )


def _create(repository: Path) -> Path:
    payload = routing._create_worktree(repository, SESSION, NODE, BASE)
    assert payload.get("ok"), payload
    tree = _launch_path(repository)
    assert tree.is_dir()
    return tree


def _retry(repository: Path) -> dict:
    token = routing.LAUNCH_RUN_ID.set(RUN)
    try:
        return routing._create_worktree(repository, SESSION, NODE, BASE)
    finally:
        routing.LAUNCH_RUN_ID.reset(token)


def _trash_entries(repository: Path, tree: Path) -> list[Path]:
    root = routing._worktree_trash_root(repository, tree)
    return sorted(root.iterdir()) if root.is_dir() else []


def _assert_refused(repository: Path, tree: Path, before: int) -> None:
    token = routing.LAUNCH_RUN_ID.set(RUN)
    try:
        with pytest.raises(CrewError) as raised:
            routing._create_worktree(repository, SESSION, NODE, BASE)
    finally:
        routing.LAUNCH_RUN_ID.reset(token)
    assert RUN in str(raised.value), raised.value
    assert tree.is_dir()
    assert _tree_inode(tree) == before
    assert _trash_entries(repository, tree) == []


def test_a_working_pointer_s_second_launch_keeps_the_tree(
    admit_repo: Path, live_pid: int
) -> None:
    tree = _create(admit_repo)
    before = _tree_inode(tree)
    _publish(admit_repo, "working", tree, pid=live_pid)

    _assert_refused(admit_repo, tree, before)


def test_a_starting_pointer_s_second_launch_keeps_the_tree(
    admit_repo: Path, live_pid: int
) -> None:
    tree = _create(admit_repo)
    before = _tree_inode(tree)
    _publish(admit_repo, "starting", tree, pid=live_pid)

    _assert_refused(admit_repo, tree, before)


def test_a_queued_pointer_leaves_the_launch_path_to_the_retry(
    admit_repo: Path,
) -> None:
    tree = _create(admit_repo)
    before = _tree_inode(tree)
    _publish(admit_repo, "queued", tree, pid=None)

    payload = _retry(admit_repo)

    assert payload.get("ok"), payload
    assert tree.is_dir()
    assert _tree_inode(tree) != before


def test_a_dead_workers_tree_is_replaced_by_the_retry(admit_repo: Path) -> None:
    tree = _create(admit_repo)
    before = _tree_inode(tree)
    _publish(admit_repo, "working", tree, pid=_dead_pid())

    payload = _retry(admit_repo)

    assert payload.get("ok"), payload
    assert tree.is_dir()
    assert _tree_inode(tree) != before
