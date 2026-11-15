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

A process-local lock is kept only while some thread holds or waits on it. A
long-lived station or admit service takes this lock once per run it touches, so
a registry that kept every path would hold one entry per run ever seen.
"""

from __future__ import annotations

import errno
import fcntl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
# Threads currently holding or waiting on each key. An entry exists exactly
# while some thread still needs the lock object under that key.
_PROCESS_LOCKS_USERS: dict[str, int] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class LockHeld(RuntimeError):  # noqa: N818 - named as the refusal states it
    """A non-blocking request found the lock held by another holder."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"lock held: {path}")


def _acquire_process_lock(path: Path) -> tuple[str, threading.Lock]:
    """Count this thread as a user of ``path``'s lock; never leaves it unseen.

    The count is incremented under the registry guard *before* the lock is
    acquired, so a thread blocked waiting on it also holds the entry open and
    the pair can only be removed by the last thread to leave.
    """
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = _PROCESS_LOCKS[key] = threading.Lock()
        _PROCESS_LOCKS_USERS[key] = _PROCESS_LOCKS_USERS.get(key, 0) + 1
        return key, lock


def _forget_process_lock(key: str) -> None:
    """Drop ``key``'s entry once no thread still holds or waits on it."""
    with _PROCESS_LOCKS_GUARD:
        remaining = _PROCESS_LOCKS_USERS[key] - 1
        if remaining:
            _PROCESS_LOCKS_USERS[key] = remaining
            return
        del _PROCESS_LOCKS_USERS[key]
        _PROCESS_LOCKS.pop(key, None)


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold one resource lock, across nodes, for the body of the block.

    ``blocking=False`` reports a holder as :class:`LockHeld` rather than
    waiting for it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    key, local = _acquire_process_lock(path)
    try:
        if not local.acquire(blocking=blocking):
            raise LockHeld(path)
        try:
            with _hold_lock(path, blocking=blocking):
                yield
        finally:
            local.release()
    finally:
        _forget_process_lock(key)


@contextmanager
def _hold_lock(path: Path, *, blocking: bool) -> Iterator[None]:
    """Take the file's flock then its record lock, for the body of the block."""
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
