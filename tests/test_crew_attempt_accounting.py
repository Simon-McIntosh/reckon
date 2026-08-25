from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from reckon import crew


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _manifest(status: str) -> str:
    return "\n".join(
        (
            "node: accounting-node",
            f"status: {status}",
            "commits: none",
            "changed_paths: none",
            "tests: not run",
            "test_logs: none",
            "artifacts: none",
            "evidence_inputs: none",
            "follow_ons: none",
            "blockers: waiting" if status == "blocked" else "blockers: none",
            "",
        )
    )


def test_live_stream_supersedes_a_terminal_manifest_until_delivery_is_rewritten(
    crew_home,
) -> None:
    run_id = "r-stream-freshness"
    manifest = crew_home / "manifest.md"
    stream = crew_home / "current.jsonl"
    manifest.write_text(_manifest("blocked"))
    stream.write_text('{"type":"event"}\n')
    manifest_time = time.time_ns() - 2_000_000
    stream_time = manifest_time + 1_000_000
    os.utime(manifest, ns=(manifest_time, manifest_time))
    os.utime(stream, ns=(stream_time, stream_time))
    record = {
        "run_id": run_id,
        "project": "proj",
        "pid": os.getpid(),
        "phase": "working",
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "log_path": str(stream),
        "node": {"time_budget": "30m"},
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    crew._write_json(crew.pointer_path(run_id), record)

    sleeps = 0

    def deliver_current_attempt(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        manifest.write_text(_manifest("complete"))
        delivered = stream_time + 1_000_000
        os.utime(manifest, ns=(delivered, delivered))

    event = crew.watch(
        "proj",
        stall_window="1h",
        poll_interval=0,
        sleeper=deliver_current_attempt,
    )

    assert sleeps == 1
    assert event["event"] == "terminal"
    assert event["manifest_status"] == "complete"


def test_resumption_records_its_stream_budget_and_elapsed_window(crew_home) -> None:
    run_id = "r-resumed-accounting"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True)
    manifest = directory / "manifest.md"
    manifest.write_text(_manifest("blocked"))
    old_stream = directory / "attempt-1.jsonl"
    old_stream.write_text('{"type":"event"}\n')
    current_stream = directory / "resume-1.jsonl"
    current_stream.write_text('{"type":"event"}\n')
    (directory / "resume-1-advice.txt").write_text(
        "Scheduler capacity is available and the fence is extended to "
        "240 MINUTES for this continuation.\n"
    )
    dispatched = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    resumed_at = dispatched + timedelta(hours=8, minutes=30)
    record = {
        "run_id": run_id,
        "project": "proj",
        "pid": None,
        "phase": "blocked",
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": dispatched.isoformat(),
        "created_at": dispatched.isoformat(),
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "log_path": str(old_stream),
        "node": {"time_budget": "55m"},
    }
    crew._write_json(crew.pointer_path(run_id), record)

    resumed = crew.record_resumption(
        run_id,
        pid=os.getpid(),
        turn=1,
        log_path=current_stream,
        stderr_path=directory / "resume-1.stderr.log",
        attempt_started_at=resumed_at.isoformat(),
        manifest_baseline_mtime_ns=manifest.stat().st_mtime_ns,
    )
    row = crew.classify_pointer(
        resumed,
        now_seconds=(resumed_at + timedelta(minutes=250)).timestamp(),
    )

    assert resumed["attempt"] == 2
    assert resumed["phase"] == "working"
    assert resumed["log_path"] == str(current_stream)
    assert resumed["attempt_budget_seconds"] == 14_400
    assert row["classification"] == "running"
    assert row["budget_seconds"] == 14_400
    assert row["elapsed_seconds"] == 15_000
    assert row["budget_overrun_seconds"] == 600


def test_resumption_without_an_extension_restarts_the_original_allowance(
    crew_home,
) -> None:
    run_id = "r-restarted-accounting"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True)
    (directory / "resume-1-advice.txt").write_text(
        "Continue in the same session. Your time budget restarts from now.\n"
    )
    started = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "attempt": 1,
            "created_at": started.isoformat(),
            "node": {"time_budget": "30m"},
        },
    )

    resumed = crew.record_resumption(
        run_id,
        pid=os.getpid(),
        turn=1,
        log_path=directory / "resume-1.jsonl",
        stderr_path=directory / "resume-1.stderr.log",
        attempt_started_at=(started + timedelta(hours=2)).isoformat(),
    )

    assert resumed["attempt_budget_seconds"] == 1_800
