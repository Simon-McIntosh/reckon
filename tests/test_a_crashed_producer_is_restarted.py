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

from reckon.crew import dispatch_watch

_WATCH_LOOP = """
import os
import time
from pathlib import Path
from reckon.cli import main
from reckon.crew import recovery, recovery_watch

events = Path(os.environ['RECKON_TEST_PRODUCER_EVENTS'])
mode = os.environ['RECKON_TEST_PRODUCER_MODE']

def watch_follow(project, **_kwargs):
    with recovery_watch._watch_registration(project, '1m') as (acquired, _watcher):
        if not acquired:
            return
        with events.open('a', encoding='utf-8') as output:
            output.write(str(os.getpid()) + '\\n')
        if mode == 'crash' or mode == 'always-crash':
            if mode == 'always-crash' or len(events.read_text().splitlines()) == 1:
                time.sleep(0.15)
                raise RuntimeError('watch loop crashed')
        if mode == 'lease':
            time.sleep(0.15)
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
    log = tmp_path / "watch.log"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_WATCH_ARMING", "on")
    monkeypatch.setenv("RECKON_TEST_PRODUCER_EVENTS", str(events))
    monkeypatch.setenv("RECKON_TEST_PRODUCER_MODE", mode)
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
