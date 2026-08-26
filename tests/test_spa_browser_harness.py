"""Contracts for the browser probe harness itself, without starting a browser."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from spa_browser_harness import _terminate_process_group, temporary_browser_profile


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _leader_with_a_child() -> tuple[subprocess.Popen[str], int]:
    """Start a process that spawns a long-lived child and outlives its own kill.

    This is the shape of the probe driver: it spawns a browser and kills it from
    a `finally` clause, which a hard timeout never reaches.
    """
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']);"
            "print(child.pid, flush=True);"
            "time.sleep(600)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    return leader, int(leader.stdout.readline().strip())


def test_timeout_reap_takes_the_spawned_child_with_the_driver() -> None:
    """A timed-out probe must not leave a browser running.

    Signalling only the driver leaves its browser holding the profile directory
    that cleanup then waits on, and consuming the machine that the next probe
    has to meet its own deadline on -- so one timeout makes the following ones
    likelier, and a suite gets slower the longer it runs.
    """
    leader, child = _leader_with_a_child()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            leader.communicate(timeout=1)
        _terminate_process_group(leader)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (_alive(leader.pid) or _alive(child)):
            time.sleep(0.05)

        assert not _alive(child), "the driver's child survived the reap"
        assert not _alive(leader.pid)
    finally:
        for pid in (child, leader.pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def test_profile_cleanup_lets_the_body_s_exception_through(tmp_path) -> None:
    """Cleanup runs in a `finally`, so it must not become an exception sink.

    A jump statement leaving that block discards whatever was propagating, which
    turned a probe timeout into an unrelated name error at the call site.
    """
    with pytest.raises(subprocess.TimeoutExpired):
        with temporary_browser_profile(tmp_path) as profile:
            assert profile.is_dir()
            raise subprocess.TimeoutExpired(cmd="probe", timeout=75)
