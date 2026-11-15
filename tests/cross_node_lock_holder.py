"""A second process that holds only a file's POSIX record lock.

``flock`` is granted by the local kernel, so a holder on another node does not
block a take here. A POSIX record lock (``lockf``) is held by the file server on
GPFS and does exclude across nodes. A test that stands this holder in for the
other node therefore takes *only* the record lock: a site that still guards
with ``flock`` alone sees the lock free and proceeds, where a site taking
``reckon.node_exclusive_lock.exclusive_lock`` sees it held.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

RECORD_LOCK_READY = "record-lock-held"

# The record lock alone, then the stand-in waits on stdin to be released.
_RECORD_LOCK_HOLDER = """
import fcntl
import sys

handle = open(sys.argv[1], "a+b")
fcntl.lockf(handle.fileno(), fcntl.LOCK_EX)
sys.stdout.write(sys.argv[2] + "\\n")
sys.stdout.flush()
sys.stdin.read()
"""


@contextmanager
def record_lock_held_elsewhere(lock_path: Path) -> Iterator[subprocess.Popen[str]]:
    """Hold ``lock_path``'s record lock from another process for the block.

    The yielded process takes no ``flock``, so only a record-lock take can
    observe it. It is released when the block exits.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", _RECORD_LOCK_HOLDER, str(lock_path), RECORD_LOCK_READY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == RECORD_LOCK_READY, (
            "the stand-in process never took the record lock"
        )
        yield holder
    finally:
        if holder.stdin is not None:
            try:
                holder.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
        holder.wait(timeout=60)