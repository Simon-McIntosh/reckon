"""Cross-node exclusion for the git-answer, lane-probe and sweep-status locks."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon.crew import recovery_watch, resumption

HELD_SECONDS = 1.5
RELEASE_SECONDS = 20.0

# A second process taking only the POSIX record lock. It reports that it holds
# the lock, then keeps it until the parent writes a line to its stdin.
HOLDER_SOURCE = """
import fcntl
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
handle = path.open("a+b")
fcntl.lockf(handle.fileno(), fcntl.LOCK_EX)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


class _Holder:
    """A second process holding one lock file with the record lock alone."""

    def __init__(self, path: Path) -> None:
        self.child = subprocess.Popen(
            [sys.executable, "-c", HOLDER_SOURCE, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.child.stdout is not None
        assert self.child.stdout.readline().strip() == "held"

    def release(self) -> None:
        try:
            if self.child.stdin is not None:
                self.child.stdin.write("go\n")
                self.child.stdin.flush()
        except OSError:
            pass
        self.child.wait(timeout=RELEASE_SECONDS)

    def kill(self) -> None:
        self.child.kill()
        self.child.wait(timeout=RELEASE_SECONDS)


@contextmanager
def record_lock_holder(path: Path):
    """Hold ``path`` in a second process with the record lock, then tear down."""
    holder = _Holder(path)
    try:
        yield holder
    finally:
        try:
            holder.release()
        except (OSError, subprocess.TimeoutExpired):
            holder.kill()


def waits_for_holder(label: str, lock_path: Path, write) -> None:
    """Show ``write`` waits while only the record lock is held."""
    with record_lock_holder(lock_path):
        finished = threading.Event()

        def run() -> None:
            try:
                write()
            finally:
                finished.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert not finished.wait(HELD_SECONDS), (
            label + " wrote while another process held only the record lock"
        )
    assert finished.wait(RELEASE_SECONDS), label + " never finished"
    thread.join(RELEASE_SECONDS)
    assert not thread.is_alive()


@pytest.fixture(name="crew_home")
def _crew_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    return tmp_path


def test_git_answer_store_waits_for_the_record_lock(crew_home, tmp_path) -> None:
    store_path = recovery_watch._git_answer_store_path()
    lock_path = store_path.with_name(store_path.name + ".lock")
    tree = str(tmp_path)

    def write() -> None:
        def mutate(store) -> None:
            store["commits"].setdefault(tree, {})["calls"] = "sha"

        recovery_watch._update_git_answer_store(mutate)

    waits_for_holder("the git-answer store update", lock_path, write)
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert stored["commits"][tree] == {"calls": "sha"}


def test_lane_probe_waits_for_the_record_lock(crew_home) -> None:
    project, backend_name = "a-project", "a-backend"
    cache_path = resumption.lane_probe_cache_path(project, backend_name)
    lock_path = cache_path.with_suffix(".lock")

    def runner(_name, _backend):
        return {"status": "served"}

    def write() -> None:
        resumption.probe_lane_availability(
            project, backend_name, {"name": backend_name}, runner=runner
        )

    waits_for_holder("the lane probe cache refresh", lock_path, write)
    observation = json.loads(cache_path.read_text(encoding="utf-8"))
    assert observation["status"] == "served"


def test_sweep_status_waits_for_the_record_lock(crew_home) -> None:
    project = "a-project"
    status_path = resumption.sweep_status_path(project)
    lock_path = status_path.with_suffix(".lock")

    def write() -> None:
        resumption._write_sweep_status(
            project, {"swept_at": "2026-01-01T00:00:00Z"}, writer="hand:test"
        )

    waits_for_holder("the sweep status write", lock_path, write)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["writers"]
