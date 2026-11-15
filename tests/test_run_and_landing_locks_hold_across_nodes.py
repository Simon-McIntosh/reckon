"""The run pointer and landing-pass locks exclude a holder on another node.

``flock`` is granted by the local kernel, so a holder on another node does not
block a take here. Both sites take ``reckon.node_exclusive_lock.exclusive_lock``,
whose POSIX record lock the file server holds across nodes. These tests stand a
second process in for the other node: it takes *only* the record lock on the
lock file, never ``flock``, so a site that still guards with ``flock`` alone
sees the lock free and proceeds.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon.crew import queue_landing, runs

# Held by the stand-in: the record lock alone, then it waits to be released.
_RECORD_LOCK_HOLDER = """
import fcntl
import sys

handle = open(sys.argv[1], "a+b")
fcntl.lockf(handle.fileno(), fcntl.LOCK_EX)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.read()
"""


@contextmanager
def record_lock_held_elsewhere(path: Path) -> Iterator[None]:
    """Hold the record lock of ``path`` from another process for the block.

    The process takes no ``flock``, so only a record-lock take can observe it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", _RECORD_LOCK_HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held", (
            "the stand-in process never took the record lock"
        )
        yield
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=30)


@pytest.fixture
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    return runs.crew_home()


def test_a_record_lock_holder_elsewhere_refuses_the_pointer(crew_home: Path) -> None:
    run_id = "record-lock-stand-in"
    lock = crew_home / "locks" / f"{run_id}.lock"

    with (
        record_lock_held_elsewhere(lock),
        pytest.raises(runs.PointerLockHeldError),
        runs._pointer_lock(run_id, blocking=False),
    ):
        pass

    # The same take succeeds once the stand-in is gone, so the refusal above
    # was the holder and not a lock this process could never take.
    with runs._pointer_lock(run_id, blocking=False):
        pass


def test_a_record_lock_holder_elsewhere_makes_the_landing_pass_busy(
    crew_home: Path,
) -> None:
    with record_lock_held_elsewhere(crew_home / "locks" / "landing-pass.lock"):
        result = queue_landing.land_once()

    assert result.get("busy") is True
    assert result["landed"] == []

    # A pass run by the only holder is not busy: the busy verdict above came
    # from the stand-in, not from the pass being unable to start here.
    assert queue_landing.land_once().get("busy") is not True
