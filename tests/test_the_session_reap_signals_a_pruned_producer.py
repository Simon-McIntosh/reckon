"""The session-end reap signals a producer whose test tree was pruned.

Under ``tmp_path_retention_policy = "failed"`` a passing test's temporary tree is
removed before the session-scoped reaper runs, taking the seat record the reaper
would find the producer by. ``preserve_seat_records`` answers that by copying
each record into a mirror the prune does not touch, and the mirror is set aside
while tests run so a test's own reap cannot read another test's records.

Both halves of that path are measured here on synthetic trees. The mirrored
record has to name the home it was written under: the mirror is not under that
home, and the reap binds a record to a producer by comparing the record's home
against the ``RECKON_HOME`` in the producer's own environment, so a home derived
from the mirror's path is a name no producer carries. And the set-aside has to
accumulate: it is drained once, at session end, so a cycle that replaced it
would leave every earlier test's records — whose homes the prune has already
removed — unreachable.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from reckon.crew.routing import signal_worker
from tests.conftest import (
    _SEAT_RECORDS_DIR,
    _restore_seat_record_mirror,
    _seat_record_mirror_aside,
    _set_aside_seat_record_mirror,
    preserve_seat_records,
    reapable_watch_pids,
)


def _start_stub_producer(home: Path, project: str) -> subprocess.Popen:
    """A live process whose own environment names ``home``, with its seat record."""
    watch_dir = home / "crew" / "watch"
    watch_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        env={**os.environ, "RECKON_HOME": str(home)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (watch_dir / f"{project}-00.lock").write_text(
        json.dumps({"pid": process.pid, "project": project}), encoding="utf-8"
    )
    return process


def _await_bound(process: subprocess.Popen, home: Path, *, bound: float = 5.0) -> None:
    """Wait until the stub's own environment names ``home``.

    ``Popen`` returns before the child has finished its ``execve``, and an
    environment read in that window is the parent's. The binding the reap reads
    is the child's own, so wait for it rather than measuring a race.
    """
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        try:
            environ = (
                Path("/proc", str(process.pid), "environ").read_bytes().split(b"\0")
            )
        except OSError:
            environ = []
        for item in environ:
            name, _, value = item.partition(b"=")
            if name == b"RECKON_HOME" and value and Path(os.fsdecode(value)) == home:
                return
        time.sleep(0.02)
    raise AssertionError(f"stub producer {process.pid} never named {home}")


def _end_stub(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)


def _clean_aside(root: Path) -> None:
    shutil.rmtree(_seat_record_mirror_aside(root), ignore_errors=True)


def test_the_session_reap_signals_a_producer_whose_home_was_pruned(
    tmp_path: Path,
) -> None:
    """The session-end reap signals by the home the mirrored record names.

    The stub's home is pruned the way a passing test's tree is pruned before
    session end, and the mirror is set aside and restored the way the fixtures
    do across a session, so the pid is read from the mirror alone. The pids
    asserted are the ones the session-end path itself signals: what
    ``reapable_watch_pids`` returns is what ``reaped_watch_producers`` sends
    ``SIGTERM`` to.
    """
    root = tmp_path / "run-root"
    home = root / "pruned-home"
    process = _start_stub_producer(home, "pruned")
    try:
        _await_bound(process, home)
        preserve_seat_records(root)
        shutil.rmtree(home)
        _set_aside_seat_record_mirror(root)
        _restore_seat_record_mirror(root)

        reaped = reapable_watch_pids(root)
        assert reaped == [process.pid], (
            "the session-end reap would not signal the producer: the mirrored "
            "record does not name the home the producer's environment names"
        )
        for pid in reaped:
            assert signal_worker(pid, signal.SIGTERM) is True
        # The stub is this test's own child, so its exit is read through the
        # wait: a signalled child stays in /proc as a zombie until it is
        # collected, and only the wait can say it ended by the signal.
        assert process.wait(timeout=10) == -signal.SIGTERM
    finally:
        _end_stub(process)
        _clean_aside(root)


def test_the_set_aside_accumulates_records_across_cycles(tmp_path: Path) -> None:
    """Every cycle's records survive to the session end, not just the last.

    The set-aside is merged at each test's setup and put back once, at session
    end. The first cycle's home is pruned before the second arms, so if the
    set-aside replaced rather than merged, the first producer's record would be
    gone by the time the session-end reap runs and nothing else could recover
    it.
    """
    root = tmp_path / "run-root"
    first_home = root / "first-home"
    second_home = root / "second-home"
    first = _start_stub_producer(first_home, "alpha")
    second = _start_stub_producer(second_home, "beta")
    try:
        _await_bound(first, first_home)
        _await_bound(second, second_home)
        preserve_seat_records(root)
        _set_aside_seat_record_mirror(root)
        shutil.rmtree(first_home)
        preserve_seat_records(root)
        _set_aside_seat_record_mirror(root)
        _restore_seat_record_mirror(root)

        mirror = root / _SEAT_RECORDS_DIR
        assert (mirror / "first-home" / "crew" / "watch" / "alpha-00.lock").exists()
        assert (mirror / "second-home" / "crew" / "watch" / "beta-00.lock").exists()
        assert set(reapable_watch_pids(root)) == {first.pid, second.pid}
    finally:
        _end_stub(first)
        _end_stub(second)
        _clean_aside(root)
