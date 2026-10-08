"""The per-test reaper ends a producer that never wrote its seat record.

A test's per-test reaper has two ways to reach a producer a test armed: the
seat record the producer writes into its own configuration home, and the
configuration home the producer's own environment names. The record is a claim
about the past and can be absent — a producer that has not yet written it when
the test ends is invisible to a reap that reads records only, and it then
survives to fail the session-end scan, which fails the whole session at
teardown.

This measures the second way directly. A stand-in process carries the argv and
environment a real ``crew watch`` producer has, lies under this test's
temporary tree, and writes no seat record at all. The record-based reap cannot
see it; ``reapable_watch_pids`` must end it anyway. The named half is a scan of
every live process, so it is scoped to a test marked ``arms_watch_producer`` and
this file asserts that scoping too.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from reckon.crew.routing import signal_worker
from tests.conftest import reapable_watch_pids, watcher_record_pids


def _spawn_unrecorded_producer(home: Path) -> subprocess.Popen:
    """A live process whose argv and environment read as a watch producer.

    The home it names lies under the test's temporary tree, so the reap that
    binds a producer by the home its environment names has it; no seat record is
    written beneath it, which is the state a producer is in between the moment
    it is armed and the moment it registers.
    """
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            "crew",
            "watch",
            "--project",
            "reaper-unrecorded-probe",
        ],
        env={**os.environ, "RECKON_HOME": str(home)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _await_names_home(
    process: subprocess.Popen, home: Path, *, bound: float = 5.0
) -> None:
    """Wait until the child's own environment names ``home``.

    ``Popen`` returns before the child has finished ``execve``, and an
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
    raise AssertionError(f"stand-in producer {process.pid} never named {home}")


def _end(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)


def test_a_live_producer_with_no_seat_record_is_unrecorded(tmp_path: Path) -> None:
    """The state the reap must handle: the producer is live and has no record.

    This is the precondition of the leak, read on the same symbols the reaper
    reads. The stand-in is live and names this test's home, yet no seat record
    exists under it, so a reap whose only path is the record cannot reach the
    producer.
    """
    home = tmp_path / "producer-home"
    process = _spawn_unrecorded_producer(home)
    try:
        _await_names_home(process, home)

        assert process.poll() is None, "the stand-in must be alive for this measure"
        assert watcher_record_pids(tmp_path) == [], (
            "the stand-in wrote no seat record, so the record path cannot reach it"
        )
    finally:
        _end(process)


def test_the_per_test_reap_signals_a_producer_that_never_wrote_a_seat_record(
    tmp_path: Path,
) -> None:
    """The repair: the reap reaches a producer by the home its environment names.

    ``reapable_watch_pids`` is the seam the per-test reaper signals. Against the
    unfixed reaper it reads records only and its result is empty while the
    stand-in is live; with the repair it names the stand-in, and the signal is
    read through the child's own ``wait`` so the exit is the one this reap sent,
    not a later reap's.
    """
    home = tmp_path / "producer-home"
    process = _spawn_unrecorded_producer(home)
    try:
        _await_names_home(process, home)
        assert process.poll() is None, "the stand-in must be live for this measure"

        reaped = reapable_watch_pids(tmp_path, include_named_half=True)
        assert process.pid in reaped, (
            "the reap does not reach a producer that never wrote a seat record; "
            "it will survive to fail the session-end scan"
        )
        for pid in reaped:
            assert signal_worker(pid, signal.SIGTERM) is True
        assert process.wait(timeout=10) == -signal.SIGTERM
    finally:
        _end(process)


def test_the_named_half_runs_only_for_a_test_that_arms_a_producer(
    tmp_path: Path,
) -> None:
    """The whole-host scan is paid only where a producer can have been armed.

    ``per_test_reap_pids`` is what the per-test fixture calls, with ``armed``
    read from the test's marker. An unmarked test arms nothing — the shared
    fixture suppresses arming for it — so its reap must not include the named
    half; a marked test's must.
    """
    from tests.conftest import per_test_reap_pids

    home = tmp_path / "producer-home"
    process = _spawn_unrecorded_producer(home)
    try:
        _await_names_home(process, home)

        assert process.pid not in per_test_reap_pids(tmp_path, armed=False), (
            "an unmarked test paid the whole-host scan for a producer it could "
            "not have armed"
        )
        assert process.pid in per_test_reap_pids(tmp_path, armed=True)
    finally:
        _end(process)
