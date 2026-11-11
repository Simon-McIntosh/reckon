"""A producer started without a service unit is restored when it loses its seat.

A host that cannot enable linger has no unit to restart the project's watcher,
so the producer runs as a plain background process under the arming supervisor.
Every case here starts its producer through that same supervised launch, and
each absence asserted below is therefore a running supervisor declining to
start one.
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

from reckon.crew import dispatch_watch, recovery_watch

# Each mode is one way a producer can leave the seat. The first takes the seat
# and then lets its own lease registration lapse, which is the state a host
# without a service unit reaches when nothing renews the producer's lease; the
# second gives the seat up as misplaced; the third never gets the seat because
# a live producer already holds it; the fourth is overtaken by another producer
# claiming the seat's lease at renewal time.
_PRODUCER = """
import json
import os
import sys
from pathlib import Path

from reckon.crew import recovery_watch, runs, watch_unit

project = os.environ['RECKON_TEST_PROJECT']
mode = os.environ['RECKON_TEST_PRODUCER_MODE']
events = Path(os.environ['RECKON_TEST_PRODUCER_EVENTS'])

def record():
    with events.open('a', encoding='utf-8') as output:
        output.write(str(os.getpid()) + '\\n')

def hold_seat():
    with recovery_watch._watch_registration(project, '1m') as (acquired, _watcher):
        if not acquired:
            record()
            sys.exit(0)
        record()
        if mode == 'lapse':
            # Nobody renewed this producer's lease for a whole interval.
            runs.update_watch_registration(project, lease_renewed_at=0.0)
            runs.renew_watch_host_lease(project)
            return
        if mode == 'misplaced':
            # The verdict a producer on a host that cannot serve the fleet
            # node's runs is given: disable the unit, give up the seat, exit 0.
            watch_unit.producer_start_decision = lambda *_a, **_k: {
                'action': 'release-and-exit',
                'project': project,
                'host': 'worker-node',
                'fleet_node': 'fleet-node',
                'runs': ['r-test'],
                'unit': 'reckon-watch-sample.service',
                'unit_disable_command': ['systemctl', '--user', 'disable', '--now', 'unit'],
                'announcement': 'seat released',
            }
            watch_unit.release_seat_when_misplaced(
                project, runner=lambda _command: None, exit_process=sys.exit
            )
            return
        if mode == 'stolen':
            # Another producer claims the seat's lease while this one holds it.
            lease = watch_unit.watch_host_lease(project)
            lease.path.write_text(
                json.dumps(
                    {'host': 'other-node', 'pid': os.getppid(), 'job': '', 'token': 't'}
                ),
                encoding='utf-8',
            )
            runs.renew_watch_host_lease(project)
            return

hold_seat()
"""


def _pids(events: Path) -> list[int]:
    return (
        [int(line) for line in events.read_text().splitlines()]
        if events.exists()
        else []
    )


def _until(predicate, message: str, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    pytest.fail(message)


def _prepare(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Point one producer fixture at a private crew home and its own log."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    events = tmp_path / "starts"
    log = tmp_path / "watch.log"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_WATCH_ARMING", "on")
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "1")
    monkeypatch.setenv("RECKON_TEST_PROJECT", "sample")
    monkeypatch.setenv("RECKON_TEST_PRODUCER_EVENTS", str(events))
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(dispatch_watch.__file__).resolve().parents[2])
    )
    return events, log


def _start(tmp_path: Path, monkeypatch, mode: str, events: Path, log: Path):
    """Start one producer under the arming supervisor, with no service unit."""
    monkeypatch.setenv("RECKON_TEST_PRODUCER_MODE", mode)
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
    return dispatch_watch._start_watch_producer("sample")


def _arm(tmp_path: Path, monkeypatch, mode: str):
    events, log = _prepare(tmp_path, monkeypatch)
    return _start(tmp_path, monkeypatch, mode, events, log), events, log


def _stop_own_processes(supervisor, events: Path) -> None:
    if supervisor.poll() is None:
        supervisor.terminate()
        try:
            supervisor.wait(timeout=3)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            supervisor.wait(timeout=3)
    for pid in _pids(events):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


def test_a_lapsed_lease_is_restarted_without_a_unit(tmp_path, monkeypatch):
    """No service unit exists, and the producer comes back anyway."""
    supervisor, events, _log = _arm(tmp_path, monkeypatch, "lapse")
    try:
        _until(lambda: bool(_pids(events)), "the first producer never started")
        # Linger is unavailable: nothing owns this seat as a service, so the
        # restart can only come from the supervisor that launched the producer.
        assert dispatch_watch.watch_state("sample")["unit"] is None
        _until(
            lambda: len(_pids(events)) >= 2,
            "the producer that lost its lease was not started again",
        )
        assert _pids(events)[1] != _pids(events)[0]
        assert supervisor.poll() is None
    finally:
        _stop_own_processes(supervisor, events)


def test_a_misplaced_release_stays_stopped(tmp_path, monkeypatch):
    """A seat given up as misplaced is not taken back by a restart."""
    supervisor, events, _log = _arm(tmp_path, monkeypatch, "misplaced")
    try:
        _until(lambda: bool(_pids(events)), "the producer never started")
        _until(lambda: supervisor.poll() is not None, "the supervisor did not stop")
        assert supervisor.returncode == 0
        assert len(_pids(events)) == 1
        assert not dispatch_watch.watch_state("sample")["watcher_live"]
    finally:
        _stop_own_processes(supervisor, events)


def test_a_refused_start_is_not_restarted(tmp_path, monkeypatch):
    """A claim refused because a live producer holds the seat starts nothing."""
    events, log = _prepare(tmp_path, monkeypatch)
    with recovery_watch._watch_registration("sample", "1m") as (held, _watcher):
        assert held, "the fixture did not take the seat it needs"
        supervisor = _start(tmp_path, monkeypatch, "refused", events, log)
        try:
            _until(lambda: bool(_pids(events)), "the producer never tried the seat")
            _until(
                lambda: supervisor.poll() is not None,
                "a producer that never got the seat was restarted",
            )
            assert supervisor.returncode == 0
            assert len(_pids(events)) == 1
        finally:
            _stop_own_processes(supervisor, events)


def test_a_seat_taken_at_renewal_is_not_restarted(tmp_path, monkeypatch):
    """A producer overtaken by another live producer does not contest the seat."""
    supervisor, events, _log = _arm(tmp_path, monkeypatch, "stolen")
    try:
        _until(lambda: bool(_pids(events)), "the producer never started")
        _until(
            lambda: supervisor.poll() is not None,
            "a producer whose seat was taken was restarted",
        )
        assert supervisor.returncode == 75
        assert len(_pids(events)) == 1
    finally:
        _stop_own_processes(supervisor, events)
