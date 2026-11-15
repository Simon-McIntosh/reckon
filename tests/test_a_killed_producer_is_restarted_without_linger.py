"""A producer killed outright comes back with no coordinator in the loop.

A host that will not enable lingering has no unit to restart a project's
watcher, and systemd is not there to notice that the process died. The producer
runs as a plain background process under the arming supervisor, so the only
thing that can start it again is that supervisor's own wait on its child.

Killing the producer is the field case: the snapshot every session of the
project reads went stale with no failed arm to point at, because an exit nobody
observed starts nothing. The restart is measured by what a reader reads — a live
producer and a snapshot written after the kill — and by counting the armings the
calling process made, so a restart this test performed itself cannot pass it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from reckon import service
from reckon.crew import dispatch_watch, watch_unit
from tests.test_crew_watch_ensure import FakeWatchService, _backend_bin, _config

# The producer the supervisor runs instead of the real watcher. It takes the
# project's seat through the same registration the watcher uses, records the pid
# that took it, and then writes a snapshot line every tick — the freshness a
# session of the project reads. It is killed from outside, so nothing here
# handles its own death.
_PRODUCER = """
import os
import sys
import time
from pathlib import Path

from reckon.crew import recovery_watch

project = os.environ['RECKON_TEST_PROJECT']
starts = Path(os.environ['RECKON_TEST_PRODUCER_STARTS'])
snapshots = Path(os.environ['RECKON_TEST_PRODUCER_SNAPSHOTS'])

with recovery_watch._watch_registration(project, '1m') as (acquired, _watcher):
    if not acquired:
        sys.exit(0)
    with starts.open('a', encoding='utf-8') as output:
        output.write(str(os.getpid()) + '\\n')
    while True:
        with snapshots.open('a', encoding='utf-8') as output:
            output.write(str(os.getpid()) + ' ' + repr(time.time()) + '\\n')
        time.sleep(0.1)
"""


class LingerlessWatchService(FakeWatchService):
    """A manager on a host that will not keep this account's units past logout.

    ``loginctl enable-linger`` is answered with the error the fleet nodes
    measured, so the arming path has to place the watcher outside the manager.
    """

    def lingering(self) -> bool:
        return False

    def enable_linger(self) -> None:
        raise service.LingerUnavailableError(
            "could not enable lingering, so the service would stop at logout: "
            "Could not enable linger: No such device or address"
        )


def _until(predicate, message: str, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    pytest.fail(message)


def _pids(starts: Path) -> list[int]:
    if not starts.exists():
        return []
    return [int(line) for line in starts.read_text().splitlines()]


def _snapshots(path: Path) -> list[tuple[int, float]]:
    if not path.exists():
        return []
    return [
        (int(pid), float(stamp))
        for pid, stamp in (line.split() for line in path.read_text().splitlines())
    ]


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _stop(supervisors: list, starts: Path) -> None:
    for supervisor in supervisors:
        if supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=3)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=3)
    for pid in _pids(starts):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


def _arm(tmp_path: Path, monkeypatch):
    """Arm through the linger fallback, the way ``crew watch --ensure`` does."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    starts = tmp_path / "starts"
    snapshots = tmp_path / "snapshots"
    log = tmp_path / "watch.log"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_WATCH_ARMING", "on")
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "1")
    monkeypatch.setenv("RECKON_TEST_PROJECT", "sample")
    monkeypatch.setenv("RECKON_TEST_PRODUCER_STARTS", str(starts))
    monkeypatch.setenv("RECKON_TEST_PRODUCER_SNAPSHOTS", str(snapshots))
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(dispatch_watch.__file__).resolve().parents[2])
    )
    monkeypatch.setattr(dispatch_watch, "_read_fleet_record", lambda: None)
    monkeypatch.setattr(
        dispatch_watch,
        "_watch_producer_argv",
        lambda _project: [
            sys.executable,
            "-c",
            dispatch_watch._WATCH_PRODUCER_SUPERVISOR,
            dispatch_watch.WATCH_LOG_ENV,
            str(log),
            "0.1",
            "0.8",
            "3.0",
            "sample",
            str(Path(dispatch_watch.__file__).resolve().parents[2]),
            sys.executable,
            "-c",
            _PRODUCER,
        ],
    )

    # Every arming this process makes, in order, so a test can say whether a
    # restart came from the caller or from the supervisor. Both the count and
    # the cleanup read this list, so a test compares its length across a point
    # rather than emptying it.
    started: list = []
    original = dispatch_watch._start_watch_producer

    def record(project: str):
        supervisor = original(project)
        started.append(supervisor)
        return supervisor

    monkeypatch.setattr(dispatch_watch, "_start_watch_producer", record)

    result = watch_unit.ensure_watcher_service(
        "sample",
        manager=LingerlessWatchService(),
        config=_config(_backend_bin(tmp_path)),
    )
    return result, started, starts, snapshots


def test_a_killed_producer_is_restarted_without_linger(tmp_path, monkeypatch):
    """SIGKILL the producer: a new one serves a fresh snapshot, uncoordinated."""
    result, started, starts, snapshots = _arm(tmp_path, monkeypatch)
    try:
        assert result["path"] == "fallback"
        assert result["watcher_live"] is True
        _until(lambda: bool(_pids(starts)), "the producer never took its seat")
        first = _pids(starts)[0]
        _until(
            lambda: any(pid == first for pid, _stamp in _snapshots(snapshots)),
            "the producer never wrote a snapshot",
        )

        # From here on an arming this process makes is the test restarting the
        # producer rather than the host doing it, and it must not happen.
        armed = len(started)
        killed_at = time.time()
        os.kill(first, signal.SIGKILL)
        _until(lambda: not _alive(first), "the killed producer outlived SIGKILL")

        def restarted() -> bool:
            return any(
                pid != first and _alive(pid) and stamp > killed_at
                for pid, stamp in _snapshots(snapshots)
            )

        _until(
            restarted,
            "no new producer served a fresh snapshot within two minutes",
            timeout=120,
        )
        assert len(started) == armed, (
            "the restart was an arming call, not the supervisor's"
        )
        assert dispatch_watch.watch_state("sample")["watcher_live"] is True
        assert _pids(starts)[-1] != first
    finally:
        _stop(started, starts)


def test_ensure_five_times_leaves_one_producer(tmp_path, monkeypatch):
    """Five ensures against a live producer start nothing and count one."""
    result, started, starts, _ = _arm(tmp_path, monkeypatch)
    try:
        assert result["watcher_live"] is True
        _until(lambda: bool(_pids(starts)), "the producer never took its seat")
        armed = len(started)
        for _ in range(5):
            state = dispatch_watch._ensure_watch_producer("sample")
            assert state["watcher_live"] is True
        assert len(started) == armed, "an ensure against a live producer armed another"
        assert len(_pids(starts)) == 1
        assert _alive(_pids(starts)[0])
    finally:
        _stop(started, starts)
