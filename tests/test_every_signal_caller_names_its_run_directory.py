"""Every signalling caller names the directory its sender record lands in.

A refusing guard writes the outcome of a signal attempt into the run directory
the caller names. A caller that pre-writes its attribution record and then calls
the signal home without the directory leaves that outcome write with nowhere to
go, so the run's sender file carries an attribution line for a SIGTERM and no
line saying whether it went out. These cases drive one production caller from
each of the three modules that signal runs — dispatch, promotion and recovery —
against a synthesised live process whose recorded start time does not match the
process actually holding the pid. The identity guard refuses, and the run
directory each caller names must end with a refused outcome.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import promotion, recovery, routing, runs
from reckon.crew.dispatch import terminate

# The kernel start tick a run record carries is a positive integer, so this
# never equals the tick of the process actually holding the pid.
STALE_START_TIME = "0"


class _LiveRun:
    """A real process and the run directory a signal attempt would record in."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.run_dir = runs.run_dir(run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=True,
        )

    def records(self) -> list[dict]:
        path = self.run_dir / routing.SENDER_RECORD_NAME
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move all crew state into the test's temporary directory."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def live_run(home: Path):
    del home  # requested for its environment, not its path
    run = _LiveRun("r-caller-names-its-directory")
    try:
        yield run
    finally:
        run.stop()


def _assert_ends_with_refusal(run: _LiveRun, *, reason: str) -> None:
    records = run.records()
    assert records, "the caller wrote no attribution record"
    outcome = records[-1]
    assert outcome.get("outcome") == "refused", (
        f"the run's sender file does not end with a refused outcome: {outcome!r}"
    )
    assert outcome["reason"] == reason
    assert outcome["target_pid"] == run.process.pid


def test_dispatch_terminate_names_the_run_directory(live_run: _LiveRun) -> None:
    """dispatch.terminate writes the refusal into the run it was asked to stop."""
    runs._write_json(
        runs.pointer_path(live_run.run_id),
        {
            "run_id": live_run.run_id,
            "pid": live_run.process.pid,
            "pid_start_time": STALE_START_TIME,
            "phase": "running",
        },
    )

    with pytest.raises(routing.CrewError):
        terminate(live_run.run_id)

    _assert_ends_with_refusal(live_run, reason="run-stop")


def test_promotion_settle_names_the_run_directory(
    live_run: _LiveRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle signal records its refusal in the run whose writer it ends."""
    manifest = live_run.run_dir / "manifest.md"
    manifest.write_text("status: complete\n", encoding="utf-8")
    record = {
        "run_id": live_run.run_id,
        "launch": "cli",
        "pid": live_run.process.pid,
        "pid_start_time": STALE_START_TIME,
        "manifest_path": str(manifest),
    }
    # A substituted liveness probe answers liveness for the module under test;
    # the identity mismatch the guard refuses on is the record's own start time.
    monkeypatch.setattr(promotion, "process_alive", lambda pid: True)

    ended = promotion._end_live_writer_for_settle(record)

    assert ended is False
    _assert_ends_with_refusal(live_run, reason="promotion-settle")


def test_recovery_watchdog_names_the_run_directory(
    live_run: _LiveRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget watchdog records its refusal in the run it tried to stop."""
    record = {
        "run_id": live_run.run_id,
        "launch": "cli",
        "phase": "running",
        "process_alive": True,
        "pid": live_run.process.pid,
        "pid_start_time": STALE_START_TIME,
    }
    monkeypatch.setattr(
        recovery,
        "_budget_timing",
        lambda record: {"budget_seconds": 10, "elapsed_seconds": 100},
    )
    config = {"fences": {"enforce_budget_watchdog": True, "budget_grace_multiple": 1.0}}

    recovery._apply_budget_watchdog(record, config)

    _assert_ends_with_refusal(live_run, reason="budget-watchdog")
