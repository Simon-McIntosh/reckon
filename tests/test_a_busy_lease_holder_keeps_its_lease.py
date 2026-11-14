"""A lease whose holder is running on this host is not taken because it went stale.

The station renews its lease from a thread every few seconds, and the lease reads
stale after thirty. A long decode on the station's main thread holds the
interpreter, the renewal lapses, and a second station claimed the "stale" lease;
the first then found another owner on its next renewal and exited. On 14
November the station log recorded that loss 124 times, and each restart threw
away a tick in progress, so the obligations snapshot sat six hours old.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import host_lease


def _write_record(path: Path, record: dict, *, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    moment = time.time() - age_seconds
    os.utime(path, (moment, moment))


def _record_for(pid: int, *, with_start: bool = True) -> dict:
    record = {
        "host": host_lease.local_host(),
        "pid": pid,
        "job": "job-a",
        "token": "token-a",
        "pid_namespace": host_lease._pid_namespace(),
    }
    if with_start:
        record["pid_start"] = host_lease._process_start(pid)
    return record


def _claimant(state: Path) -> host_lease.HostLease:
    return host_lease.HostLease(
        state, "station", host_lease.local_host(), os.getpid(), "job-b"
    )


@pytest.fixture
def sleeper():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield process
    process.kill()
    process.wait(timeout=10)


def test_a_running_holder_keeps_a_stale_lease(tmp_path, sleeper):
    path = tmp_path / "leases" / "station.json"
    _write_record(path, _record_for(sleeper.pid), age_seconds=60)
    assert _claimant(tmp_path).claim() is False
    assert json.loads(path.read_text())["pid"] == sleeper.pid


def test_a_dead_holders_stale_lease_is_taken(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    record = _record_for(process.pid)
    process.wait(timeout=10)
    path = tmp_path / "leases" / "station.json"
    _write_record(path, record, age_seconds=60)
    claimant = _claimant(tmp_path)
    assert claimant.claim() is True
    assert json.loads(path.read_text())["pid"] == os.getpid()


def test_a_reused_pid_does_not_hold_the_lease(tmp_path, sleeper):
    record = _record_for(sleeper.pid)
    record["pid_start"] = "1"
    path = tmp_path / "leases" / "station.json"
    _write_record(path, record, age_seconds=60)
    assert _claimant(tmp_path).claim() is True


def test_a_record_without_a_start_time_is_judged_by_staleness(tmp_path, sleeper):
    path = tmp_path / "leases" / "station.json"
    _write_record(path, _record_for(sleeper.pid, with_start=False), age_seconds=60)
    assert _claimant(tmp_path).claim() is True


def test_a_fresh_lease_is_kept_as_before(tmp_path, sleeper):
    path = tmp_path / "leases" / "station.json"
    _write_record(path, _record_for(sleeper.pid, with_start=False), age_seconds=1)
    assert _claimant(tmp_path).claim() is False
