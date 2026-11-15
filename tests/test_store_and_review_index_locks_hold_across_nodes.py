"""The store and review-index locks exclude a holder on another node.

``flock`` is granted by the local kernel, so a holder on another node never
excludes ours. ``reckon.node_exclusive_lock.exclusive_lock`` adds a POSIX record
lock, which the file server holds, so it does. Each test here runs the holder in
a second process that takes only ``fcntl.lockf`` on the lock file and shows the
code under test seeing that holder.
"""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path

from reckon import _store
from reckon.crew.review_records import _index_lock

# Takes only the POSIX record lock on the path it is given, reports that it
# holds it, and keeps the descriptor open until its stdin closes. flock is
# deliberately not taken: the file server carries no flock between nodes.
HOLDER = textwrap.dedent(
    """
    import fcntl, sys
    with open(sys.argv[1], "a+b") as handle:
        fcntl.lockf(handle.fileno(), fcntl.LOCK_EX)
        sys.stdout.write("locked\\n")
        sys.stdout.flush()
        sys.stdin.read()
    """
)


@contextlib.contextmanager
def _record_lock_held_elsewhere(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    line = process.stdout.readline().strip()
    if line != "locked":
        process.kill()
        process.wait(timeout=10)
        raise AssertionError(f"holder did not report its lock: {line!r}")
    try:
        yield
    finally:
        process.stdin.close()
        process.kill()
        process.wait(timeout=10)


def test_review_index_lock_reports_a_record_lock_held_elsewhere(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "store"
    directory.mkdir()
    lock_path = directory / ".review-index" / "lock"
    with (
        _record_lock_held_elsewhere(lock_path),
        _index_lock(directory, wait=False) as locked,
    ):
        assert locked is None


def test_store_path_lock_waits_for_a_record_lock_held_elsewhere(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    target = tmp_path / "project" / "index.json"
    identity = hashlib.sha256(str(target.resolve()).encode()).hexdigest()
    lock_path = _store._config_home() / "locks" / "index" / f"{identity}.lock"

    entered = threading.Event()

    def writer() -> None:
        with _store._serialized_path_lock(target, "index"):
            entered.set()

    with _record_lock_held_elsewhere(lock_path):
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        assert not entered.wait(0.5), "the record-lock holder did not exclude us"
    assert entered.wait(10), "the lock was not released when the holder closed"
    thread.join(timeout=10)
