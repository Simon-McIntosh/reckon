"""A finished run is charged for the worker, not for the wait to promote it.

The elapsed figure a reader sees for a run whose process has ended is measured
to the run's own stream completion — the same stamp promotion writes to the
ledger — so a coordinator promoting late cannot keep inflating the overrun.
A run that is still running measures to now, so the wall-clock ceiling that
protects the fleet from a hang is unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon.crew import recovery


def _write_stream(path: Path, *events: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def _cli_record(
    run_id: str,
    *,
    started: datetime,
    log: Path,
    budget: str,
    process_alive: bool,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": budget},
        "backend": "clive",
        "launch": "cli",
        "argv": ["claude"],
        "log_path": str(log),
        "created_at": started.isoformat(),
        "process_alive": process_alive,
    }


def test_finished_run_elapsed_is_measured_to_its_stream_completion(tmp_path) -> None:
    """A finished run keeps one elapsed figure no matter when it is read.

    The fixture is the measured defect: the run ended at 2078s against a 2400s
    budget, but reported elapsed grew to 3651 with a false overrun the longer
    its coordinator waited to promote it. Read at two later observation times,
    the elapsed must be the same 2078s in both, inside the budget.
    """
    started = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    stream = _write_stream(
        tmp_path / "stream.jsonl",
        {
            "type": "system",
            "subtype": "init",
            "session_id": "s-1",
            "timestamp": "2026-09-05T12:00:00Z",
        },
        {"type": "assistant", "session_id": "s-1", "timestamp": "2026-09-05T12:00:05Z"},
        {
            "type": "result",
            "is_error": False,
            "session_id": "s-1",
            "timestamp": "2026-09-05T12:34:38Z",
        },
    )
    record = _cli_record(
        "r-finished",
        started=started,
        log=stream,
        budget="40m",
        process_alive=False,
    )

    early = recovery._budget_timing(
        record, now_seconds=(started + timedelta(seconds=3000)).timestamp()
    )
    late = recovery._budget_timing(
        record, now_seconds=(started + timedelta(seconds=3651)).timestamp()
    )

    assert early["budget_seconds"] == 2400
    assert early["elapsed_seconds"] == 2078
    assert late["elapsed_seconds"] == 2078
    assert early["budget_overrun"] is False
    assert late["budget_overrun"] is False
    assert early["budget_overrun_seconds"] == 0


def test_still_running_record_measures_to_now_and_flags_past_budget(tmp_path) -> None:
    """A live process never adopts its stream's last event as a finish.

    A quiet-but-alive run sits inside its budget without generating, so its
    elapsed must track the clock, not the last event; otherwise a legitimate
    wait would undercount and a hang could escape the wall-clock ceiling.
    """
    started = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    stream = _write_stream(
        tmp_path / "stream.jsonl",
        {"type": "assistant", "session_id": "s-1", "timestamp": "2026-09-05T12:10:00Z"},
    )
    record = _cli_record(
        "r-live",
        started=started,
        log=stream,
        budget="10m",
        process_alive=True,
    )

    at_601 = recovery._budget_timing(
        record, now_seconds=(started + timedelta(seconds=601)).timestamp()
    )
    at_900 = recovery._budget_timing(
        record, now_seconds=(started + timedelta(seconds=900)).timestamp()
    )

    assert at_601["elapsed_seconds"] == 601
    assert at_900["elapsed_seconds"] == 900
    assert at_601["budget_overrun"] is True
    assert at_900["budget_overrun"] is True


def test_watchdog_still_stops_a_live_over_grace_cli_worker(monkeypatch) -> None:
    """The hang ceiling is untouched: a live worker over grace is still stopped."""
    started = datetime.now(tz=UTC) - timedelta(seconds=21)
    record: dict[str, Any] = {
        "run_id": "r-watchdog",
        "launch": "cli",
        "backend": "alpha",
        "argv": ["claude"],
        "pid": 4242,
        "pid_start_time": "start",
        "phase": "working",
        "created_at": started.isoformat(),
        "attempt_started_at": started.isoformat(),
        "node": {"id": "slow-node", "plan": "plan-a", "time_budget": "10s"},
        "process_alive": True,
        "log_path": str(Path("/nonexistent/stream.jsonl")),
    }
    signalled: list[int] = []
    monkeypatch.setattr(
        recovery, "_signal_process_group", lambda pid, started_at: signalled.append(pid)
    )
    config = {"fences": {"enforce_budget_watchdog": True, "budget_grace_multiple": 2.0}}

    recovery._apply_budget_watchdog(record, config)

    assert signalled == [4242]
    assert record["phase"] == "stopped"
    assert record["watchdog_enforced"] is True


def test_with_neither_stamp_nor_budget_returns_the_null_shape(tmp_path) -> None:
    """An unreadable budget keeps the null shape instead of raising."""
    record = {
        "run_id": "r-null",
        "created_at": "2026-09-05T12:00:00Z",
        "node": {"id": "n", "plan": "plan-a", "time_budget": "half an hour"},
        "process_alive": False,
    }

    shape = recovery._budget_timing(record)

    assert shape == {
        "budget_seconds": None,
        "elapsed_seconds": None,
        "budget_overrun": False,
        "budget_overrun_seconds": 0,
    }
