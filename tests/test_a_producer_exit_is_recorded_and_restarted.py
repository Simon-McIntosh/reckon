"""A producer leaves an exit receipt before surrendering its seat."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from reckon.crew import dispatch_watch, recovery_watch, runs, watch_unit


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    home = tmp_path / "crew-home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(watch_unit.WATCH_LOG_ENV, raising=False)
    monkeypatch.setattr(runs, "_publish_watch_stream", lambda *_: None)
    monkeypatch.setattr(recovery_watch, "release_seat_when_misplaced", lambda *_: None)
    return home


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (None, 0, "watch stream ended"),
        (
            RuntimeError("stub producer crashed"),
            1,
            "RuntimeError: stub producer crashed",
        ),
        (SystemExit(75), 75, "SystemExit: 75"),
        (KeyboardInterrupt(), 130, "KeyboardInterrupt"),
    ],
)
def test_exit_is_recorded_before_seat_is_released(
    crew_home, monkeypatch, error, status, reason
):
    released = []
    original = runs.drop_watch_seat

    def release(project, handle):
        receipt = runs.read_watch_registration(project).get("exit_record")
        released.append(receipt)
        original(project, handle)

    monkeypatch.setattr(runs, "drop_watch_seat", release)
    expected = nullcontext() if error is None else pytest.raises(type(error))
    with (
        expected as caught,
        recovery_watch._watch_registration("sample", "1m") as (acquired, watcher),
    ):
        assert acquired
        watcher["last_stage"] = "classifying"
        if error is not None:
            raise error
    if error is not None:
        assert caught.value is error
    assert released and released[0] is not None, "seat released without an exit record"
    receipt = runs.read_watch_registration("sample")["exit_record"]
    assert receipt == released[0]
    assert receipt["exit_status"] == status
    assert receipt["last_stage"] == "classifying"
    assert receipt["reason"] == reason
    assert receipt["pid"] == os.getpid()
    assert receipt["exited_at"] >= receipt["started_at_epoch"]
    assert runs._probe_seat_read_only("sample")[0] is False


_STUB_PRODUCER = """
import os
import signal
import time
from pathlib import Path
from reckon.crew import recovery_watch, runs

def stop(signum, _frame):
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, stop)
runs._publish_watch_stream = lambda *_: None
recovery_watch.release_seat_when_misplaced = lambda *_: None
attempts = Path(os.environ['PRODUCER_TEST_ATTEMPTS'])
first = not attempts.exists()
with recovery_watch._watch_registration('sample', '1m') as (acquired, watcher):
    assert acquired
    with attempts.open('a') as stream:
        stream.write(str(os.getpid()) + '\\n')
    if first:
        watcher['last_stage'] = 'lease-check'
        runs.update_watch_registration(
            'sample', lease_renewed_at=time.time() - runs.producer_lease_seconds()
        )
        runs.watch_work_is_live = lambda _project: True
        runs.end_lapsed_watch_seat('sample')
    while True:
        time.sleep(0.05)
"""


def test_lapsed_producer_is_restarted_and_recorded_within_its_lease(
    crew_home, monkeypatch, tmp_path
):
    attempts = tmp_path / "attempts"
    monkeypatch.setenv("PRODUCER_TEST_ATTEMPTS", str(attempts))
    monkeypatch.delenv(runs.PRODUCER_LEASE_ENV, raising=False)
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(watch_unit.__file__).resolve().parents[2])
    )
    monkeypatch.setattr(dispatch_watch, "_watch_executable", lambda: sys.executable)
    argv = dispatch_watch._watch_producer_argv("sample")
    argv[10:] = [sys.executable, "-c", _STUB_PRODUCER]
    supervisor = subprocess.Popen(argv)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            pids = attempts.read_text().splitlines() if attempts.exists() else []
            if len(pids) >= 2:
                break
            assert supervisor.poll() is None, "supervisor ended before restarting"
            time.sleep(0.05)
        assert len(pids) == 2, "producer did not restart"
        receipt = runs.read_watch_registration("sample")
        restart = receipt["restart_record"]
        assert restart["previous_exit"]["pid"] == int(pids[0])
        assert restart["pid"] == receipt["pid"] == int(pids[1])
        assert restart["parent_pid"] == supervisor.pid
        assert restart["previous_exit"]["exit_status"] == runs.WATCH_SEAT_LOST_EXIT
        assert restart["previous_exit"]["last_stage"] == "lease-check"
        elapsed = restart["restarted_at"] - restart["previous_exit"]["lease_lapsed_at"]
        assert 0 <= elapsed <= runs.producer_lease_seconds()
        assert runs._probe_seat_read_only("sample")[0] is True
        print(
            json.dumps(
                {
                    "restart_delay_seconds": elapsed,
                    "lease_seconds": runs.producer_lease_seconds(),
                    "restart": restart,
                }
            )
        )
    finally:
        if supervisor.poll() is None:
            supervisor.send_signal(signal.SIGTERM)
        supervisor.wait(timeout=10)
    stopped = runs.read_watch_registration("sample")["exit_record"]
    assert stopped["pid"] == int(pids[1])
    assert stopped["exit_status"] == 128 + signal.SIGTERM


def test_registration_is_not_overwritten_after_a_successor_takes_the_seat(
    crew_home,
):
    with recovery_watch._watch_registration("sample", "1m") as (acquired, watcher):
        assert acquired
        runs.update_watch_registration("sample", pid=watcher["pid"] + 1)
    assert runs.read_watch_registration("sample").get("exit_record") is None


@pytest.mark.parametrize("expose_record", [True, False])
def test_ticker_exit_records_the_actual_wait_stage(
    crew_home, monkeypatch, expose_record
):
    if not expose_record:
        original = recovery_watch._watch_registration

        @contextmanager
        def registration(project, *args, **kwargs):
            with original(project, *args, **kwargs) as (acquired, _watcher):
                yield acquired, project

        monkeypatch.setattr(recovery_watch, "_watch_registration", registration)

    def interrupted(_interval):
        raise RuntimeError("wait interrupted")

    with pytest.raises(RuntimeError, match="wait interrupted"):
        list(recovery_watch.watch_ticker("sample", sleeper=interrupted))
    receipt = runs.read_watch_registration("sample")["exit_record"]
    assert receipt["last_stage"] == ("waiting" if expose_record else "registered")
    assert receipt["exit_status"] == 1
    assert receipt["reason"] == "RuntimeError: wait interrupted"


def test_placement_release_records_exit_before_dropping_the_seat(
    crew_home, monkeypatch
):
    receipts = []
    original = runs.drop_watch_seat

    def release(project, handle):
        receipts.append(runs.read_watch_registration(project).get("exit_record"))
        original(project, handle)

    monkeypatch.setattr(runs, "drop_watch_seat", release)
    with recovery_watch._watch_registration("sample", "1m") as (acquired, _watcher):
        assert acquired
        assert watch_unit.release_watch_seat("sample")
    assert receipts[0]["exit_status"] == 0
    assert receipts[0]["last_stage"] == "placement-check"
    assert runs.read_watch_registration("sample")["exit_record"] == receipts[0]


def test_reentry_by_the_same_process_is_not_a_restart(crew_home):
    for _ in range(2):
        with recovery_watch._watch_registration("sample", "1m") as (acquired, _watcher):
            assert acquired
            record = runs.read_watch_registration("sample")
            assert record.get("exit_record") is None
            assert "restart_record" not in record
