"""The fallback supervisor keeps a crashed project's watch seat occupied."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from reckon.crew import dispatch_watch, fleet_supervisor

_WATCH_LOOP = """
import os
import time
from pathlib import Path
from reckon.cli import main
from reckon.crew import recovery, recovery_watch

events = Path(os.environ['RECKON_TEST_PRODUCER_EVENTS'])
times = Path(os.environ['RECKON_TEST_PRODUCER_TIMES'])
mode = os.environ['RECKON_TEST_PRODUCER_MODE']

def watch_follow(project, **_kwargs):
    with recovery_watch._watch_registration(project, '1m') as (acquired, _watcher):
        if not acquired:
            return
        with events.open('a', encoding='utf-8') as output:
            output.write(str(os.getpid()) + '\\n')
        attempt = len(events.read_text().splitlines())
        with times.open('a', encoding='utf-8') as output:
            output.write(f'start {attempt} {time.monotonic()}\\n')
        crash = (mode == 'crash' and attempt == 1) or mode == 'always-crash'
        crash = crash or (mode == 'steady-reset' and attempt <= 5)
        if crash:
            time.sleep(float(os.environ['RECKON_TEST_STEADY_SLEEP']) if
                       mode == 'steady-reset' and attempt == 3 else 0.15)
            with times.open('a', encoding='utf-8') as output:
                output.write(f'exit {attempt} {time.monotonic()}\\n')
            raise RuntimeError('watch loop crashed')
        if mode == 'lease':
            from reckon.crew import runs
            def expired(_project):
                with times.open('a', encoding='utf-8') as output:
                    output.write('lease-check\\n')
                return time.time() - 10
            runs.watch_lease_renewed_at = expired
            runs.producer_lease_seconds = lambda: 1
            return
        while True:
            time.sleep(0.05)
    yield

recovery.watch_follow = watch_follow
main()
"""


def _pids(events: Path) -> list[int]:
    return (
        [int(line) for line in events.read_text().splitlines()]
        if events.exists()
        else []
    )


def _event_time(path: Path, kind: str, attempt: int) -> float:
    for line in path.read_text().splitlines():
        event, count, stamp = line.split()
        if event == kind and int(count) == attempt:
            return float(stamp)
    raise AssertionError(f"no {kind} event for launch {attempt}")


def _until(predicate, message: str, *, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    pytest.fail(message)


def _arm(tmp_path: Path, monkeypatch, mode: str):
    home = tmp_path / "home"
    home.mkdir()
    events = tmp_path / "starts"
    times = tmp_path / "times"
    log = tmp_path / "watch.log"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_WATCH_ARMING", "on")
    monkeypatch.setenv("RECKON_TEST_PRODUCER_EVENTS", str(events))
    monkeypatch.setenv("RECKON_TEST_PRODUCER_TIMES", str(times))
    monkeypatch.setenv("RECKON_TEST_PRODUCER_MODE", mode)
    monkeypatch.setenv("RECKON_TEST_STEADY_SLEEP", "3.25")
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
            "0.3",
            "1.2",
            "3.0",
            sys.executable,
            "-c",
            _WATCH_LOOP,
            "crew",
            "watch",
            "--project",
            "sample",
        ],
    )
    supervisor = dispatch_watch._start_watch_producer("sample")
    return supervisor, events, log


def test_watch_argv_passes_the_fleet_backoff_curve(monkeypatch):
    monkeypatch.setattr(dispatch_watch, "_watch_executable", lambda: "/bin/true")
    argv = dispatch_watch._watch_producer_argv("sample")
    assert argv[5:8] == [
        str(fleet_supervisor.FIRST_BACKOFF_SECONDS),
        str(fleet_supervisor.BACKOFF_CAP_SECONDS),
        str(fleet_supervisor.STEADY_RUN_SECONDS),
    ]


def _stop_own_processes(supervisor: subprocess.Popen, events: Path) -> None:
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


@pytest.mark.parametrize("mode", ["crash", "kill"])
def test_crashed_producer_reoccupies_the_seat(tmp_path, monkeypatch, mode):
    supervisor, events, _log = _arm(tmp_path, monkeypatch, mode)
    try:
        _until(lambda: bool(_pids(events)), "first producer never held the seat")
        first = _pids(events)[0]
        if mode == "kill":
            os.kill(first, signal.SIGKILL)
        _until(lambda: len(_pids(events)) >= 2, "crashed producer was not restarted")
        second = _pids(events)[1]
        assert second != first
        if mode == "crash":
            elapsed = _event_time(tmp_path / "times", "start", 2) - _event_time(
                tmp_path / "times", "exit", 1
            )
            assert 0.3 <= elapsed < 3.0
        _until(
            lambda: (
                dispatch_watch.watch_state("sample")["watcher_live"]
                and dispatch_watch.watch_state("sample")["watcher"]["pid"] == second
            ),
            "replacement did not hold the watch seat",
        )
        assert supervisor.poll() is None
    finally:
        _stop_own_processes(supervisor, events)


@pytest.mark.parametrize("stop", ["lease", "producer-term", "supervisor-term"])
def test_deliberate_stop_is_not_restarted(tmp_path, monkeypatch, stop):
    mode = "lease" if stop == "lease" else "hold"
    supervisor, events, _log = _arm(tmp_path, monkeypatch, mode)
    try:
        _until(lambda: bool(_pids(events)), "producer never held the seat")
        if stop == "producer-term":
            os.kill(_pids(events)[0], signal.SIGTERM)
        elif stop == "supervisor-term":
            supervisor.send_signal(signal.SIGTERM)
        _until(lambda: supervisor.poll() is not None, "supervisor did not stop")
        assert len(_pids(events)) == 1
        assert supervisor.returncode == (0 if stop == "lease" else 143)
        if stop == "lease":
            assert "lease-check" in (tmp_path / "times").read_text()
        assert not dispatch_watch.watch_state("sample")["watcher_live"]
    finally:
        _stop_own_processes(supervisor, events)


def test_repeated_crashes_reach_a_reported_limit(tmp_path, monkeypatch):
    supervisor, events, log = _arm(tmp_path, monkeypatch, "always-crash")
    try:
        _until(
            lambda: supervisor.poll() is not None,
            "restart limit was not reached",
            timeout=20,
        )
        assert len(_pids(events)) == 5
        assert supervisor.returncode == 1
        assert "restart limit reached" in log.read_text()
        assert not dispatch_watch.watch_state("sample")["watcher_live"]
    finally:
        _stop_own_processes(supervisor, events)


def test_a_steady_run_resets_the_delay_and_crash_count(tmp_path, monkeypatch):
    supervisor, events, _log = _arm(tmp_path, monkeypatch, "steady-reset")
    try:
        _until(
            lambda: len(_pids(events)) >= 6,
            "a steady producer crash exhausted the short-crash bound",
            timeout=25,
        )
        assert supervisor.poll() is None
        times = tmp_path / "times"
        first_gap = _event_time(times, "start", 2) - _event_time(times, "exit", 1)
        steady_gap = _event_time(times, "start", 4) - _event_time(times, "exit", 3)
        assert 0.3 <= steady_gap < first_gap + 0.5
        assert (
            dispatch_watch.watch_state("sample")["watcher"]["pid"] == _pids(events)[5]
        )
    finally:
        _stop_own_processes(supervisor, events)
