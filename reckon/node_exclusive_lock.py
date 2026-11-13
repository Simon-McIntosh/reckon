"""Exclusive locks for plan-state resources that hold across nodes.

A plan resource is read and written from more than one node: the login node
serves and writes plan state, and orchestrator sessions run their MCP servers
on the fleet node. ``flock`` is granted by the local kernel, so a holder on one
node never blocks a reader on another, and a recovery pass on the second node
can rewrite a resource while a move holds it.

POSIX record locks (``lockf``) are held by the file server on GPFS and were
measured to exclude across nodes, so that is the lock a writer takes.
``flock`` is taken first and released last: it excludes holders still running
the previous primitive on this node, which the two lock families do not do for
each other. Both are blocking, in the same order everywhere, so no pair of
holders can deadlock.

POSIX record locks belong to the process and are released by closing any
descriptor for the file, so a second thread of one process would take the
"same" lock and then release the first thread's on exit. A process-local lock
per path is taken before the file is opened, which keeps exclusion between two
threads (and two opens) no weaker than it was.
"""

from __future__ import annotations

import errno
import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class LockHeld(RuntimeError):  # noqa: N818 - named as the refusal states it
    """A non-blocking request found the lock held by another holder."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"lock held: {path}")


def _process_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold one resource lock, across nodes, for the body of the block.

    ``blocking=False`` reports a holder as :class:`LockHeld` rather than
    waiting for it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    local = _process_lock(path)
    if not local.acquire(blocking=blocking):
        raise LockHeld(path)
    try:
        with path.open("a+b") as handle:
            descriptor = handle.fileno()
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise LockHeld(path) from exc
            try:
                try:
                    fcntl.lockf(
                        descriptor,
                        fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except OSError as exc:
                    if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                        raise LockHeld(path) from exc
                    raise
                try:
                    yield
                finally:
                    fcntl.lockf(descriptor, fcntl.LOCK_UN)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        local.release()
