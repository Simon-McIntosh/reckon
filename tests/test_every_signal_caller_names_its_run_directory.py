"""Every signalling caller names the directory its sender record lands in.

Each caller of the shared signal home names the run directory the sender
records go to, so one signal leaves exactly one attribution record and one
outcome record there. A refusing guard writes its outcome into that same
directory, so a refused signal leaves exactly one refused record. These cases
drive one production caller from each of the three modules that signal runs —
dispatch, promotion and recovery — and assert the run's sender file ends with
the exact record set the signal produced.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import promotion, promotion_checks, recovery, routing, runs
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

    def start_time(self) -> str:
        """The recorded start tick that lets the identity guard deliver."""
        tick = routing._process_start_time(self.process.pid)
        assert tick, "the live process has no readable start time"
        return tick

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


def _assert_single_refusal(run: _LiveRun, *, reason: str) -> None:
    """A refused signal leaves exactly one record, and it is the refusal."""
    records = run.records()
    assert len(records) == 1, f"expected one refused record, got {records!r}"
    outcome = records[-1]
    assert outcome.get("outcome") == "refused", (
        f"the run's sender file does not end with a refused outcome: {outcome!r}"
    )
    assert outcome["reason"] == reason
    assert outcome["target_pid"] == run.process.pid


def _assert_one_attribution_and_one_outcome(run: _LiveRun, *, reason: str) -> None:
    """A delivered signal leaves one attribution record and one delivered outcome.

    The count is the whole point: a caller that pre-writes its own attribution
    beside the shared writer's produces two attribution records for one signal,
    which the attribution scan then reads as two senders.
    """
    records = run.records()
    attributions = [record for record in records if "outcome" not in record]
    outcomes = [record for record in records if record.get("outcome") == "delivered"]
    assert len(attributions) == 1, (
        f"a delivered signal left {len(attributions)} attribution records, "
        f"expected one: {records!r}"
    )
    assert len(outcomes) == 1, (
        f"a delivered signal left {len(outcomes)} delivered outcomes, "
        f"expected one: {records!r}"
    )
    assert len(records) == 2, f"unexpected records for one signal: {records!r}"
    assert attributions[0]["reason"] == reason
    assert attributions[0]["target_pid"] == run.process.pid
    assert outcomes[0]["reason"] == reason
    assert outcomes[0]["target_pid"] == run.process.pid


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

    _assert_single_refusal(live_run, reason="run-stop")


def test_dispatch_terminate_leaves_one_attribution_and_one_outcome(
    live_run: _LiveRun,
) -> None:
    """The delivered stop writes one attribution and one outcome into the run."""
    runs._write_json(
        runs.pointer_path(live_run.run_id),
        {
            "run_id": live_run.run_id,
            "pid": live_run.process.pid,
            "pid_start_time": live_run.start_time(),
            "phase": "running",
        },
    )

    terminate(live_run.run_id)

    _assert_one_attribution_and_one_outcome(live_run, reason="run-stop")


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
    monkeypatch.setattr(promotion_checks, "process_alive", lambda pid: True)

    ended = promotion._end_live_writer_for_settle(record)

    assert ended is False
    _assert_single_refusal(live_run, reason="promotion-settle")


def test_promotion_settle_leaves_one_attribution_and_one_outcome(
    live_run: _LiveRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivered settle writes one attribution and one outcome."""
    manifest = live_run.run_dir / "manifest.md"
    manifest.write_text("status: complete\n", encoding="utf-8")
    record = {
        "run_id": live_run.run_id,
        "launch": "cli",
        "pid": live_run.process.pid,
        "pid_start_time": live_run.start_time(),
        "manifest_path": str(manifest),
    }
    monkeypatch.setattr(promotion_checks, "process_alive", lambda pid: True)

    ended = promotion._end_live_writer_for_settle(record)

    assert ended is True
    _assert_one_attribution_and_one_outcome(live_run, reason="promotion-settle")


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

    _assert_single_refusal(live_run, reason="budget-watchdog")


def test_recovery_watchdog_leaves_one_attribution_and_one_outcome(
    live_run: _LiveRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivered watchdog stop writes one attribution and one outcome."""
    record = {
        "run_id": live_run.run_id,
        "launch": "cli",
        "phase": "running",
        "process_alive": True,
        "pid": live_run.process.pid,
        "pid_start_time": live_run.start_time(),
    }
    monkeypatch.setattr(
        recovery,
        "_budget_timing",
        lambda record: {"budget_seconds": 10, "elapsed_seconds": 100},
    )
    config = {"fences": {"enforce_budget_watchdog": True, "budget_grace_multiple": 1.0}}

    recovery._apply_budget_watchdog(record, config)

    _assert_one_attribution_and_one_outcome(live_run, reason="budget-watchdog")


def test_recovery_delivered_review_stop_names_the_run_directory(
    live_run: _LiveRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivered-review stop records its refusal in the review's run directory."""
    pointer = {
        "run_id": live_run.run_id,
        "pid": live_run.process.pid,
        "pid_start_time": STALE_START_TIME,
        "phase": "active",
        "log_path": str(live_run.run_dir / "stream.jsonl"),
        "manifest_path": str(live_run.run_dir / "manifest.md"),
    }
    monkeypatch.setattr(
        recovery,
        "review_delivered",
        lambda record: {"delivered_at": 0.0, "record_path": "", "manifest_path": ""},
    )
    monkeypatch.setattr(recovery, "_run_stream_mtime", lambda record: 1000.0)

    stopped = recovery._stop_delivered_reviews([pointer], grace_seconds=1.0)

    assert stopped == []
    _assert_single_refusal(live_run, reason="delivered-review-outlived-grace")
