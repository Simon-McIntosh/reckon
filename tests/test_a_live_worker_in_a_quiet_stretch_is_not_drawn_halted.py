"""A resumed worker is aged by the stream it is writing now."""

from __future__ import annotations

import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery_classification, recovery_liveness, runs
from reckon.crew.ticker import PANE_STATES

STALL_SECONDS = 900
QUIET_SECONDS = 7200
STREAM_RECORD = (
    '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}\n'
)


@pytest.fixture
def resumed_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, float]:
    """A stale first stream beside the fresh output of a resumed attempt."""
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    assert runs.crew_home().is_relative_to(home)
    if os.environ.get("RECKON_FIRST_STREAM_ONLY"):

        def first_stream_only(directory: Path, *, include: tuple) -> tuple[Path, float]:
            first = Path(include[0])
            return first, first.stat().st_mtime

        monkeypatch.setattr(recovery_liveness, "newest_stream", first_stream_only)
    run_id = "r-quiet-resumed"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    moment = time.time()
    first = directory / "stream.jsonl"
    resumed = directory / "resume-1.jsonl"
    first.write_text(STREAM_RECORD, encoding="utf-8")
    resumed.write_text(STREAM_RECORD, encoding="utf-8")
    stale = moment - QUIET_SECONDS
    os.utime(first, (stale, stale))
    os.utime(resumed, (moment - 1, moment - 1))
    pointer = {
        "run_id": run_id,
        "project": "quiet-fixture",
        "node": {"id": run_id, "time_budget": "20m"},
        "phase": "working",
        "created_at": datetime.fromtimestamp(stale, UTC).isoformat(),
        "launcher_host": socket.gethostname(),
        "pid": os.getpid(),
        "pid_start_time": recovery_liveness._process_start_time(os.getpid()),
        "process_alive": False,
        "log_path": str(first),
        "manifest_path": str(directory / "manifest.md"),
        "worktree": str(tmp_path / "tree"),
    }
    return pointer, moment


def _reading(pointer: dict, moment: float) -> dict:
    return recovery_classification.classify_pointer(
        pointer, now_seconds=moment, stale_after_seconds=STALL_SECONDS
    )


def test_a_live_supervisor_with_fresh_resume_output_is_working(
    resumed_pointer: tuple[dict, float],
) -> None:
    pointer, moment = resumed_pointer
    row = _reading(pointer, moment)

    print(
        f"supervisor_alive={row['process_alive']} "
        f"classification={row['classification']} state={row['fleet_verdict']['state']}"
    )
    assert row["process_alive"] is True
    assert row["liveness_proven"] is True
    assert row["fleet_verdict"]["state"] == "working", row["fleet_verdict"]


def test_fresh_resume_output_keeps_unknown_liveness_working(
    resumed_pointer: tuple[dict, float],
) -> None:
    pointer, moment = resumed_pointer
    pointer["launcher_host"] = "other-host.invalid"
    pointer["process_alive"] = None
    row = _reading(pointer, moment)

    assert row["process_alive"] is None
    assert row["fleet_verdict"]["state"] == "working", row["fleet_verdict"]


def test_no_live_process_and_stale_output_is_halted(
    resumed_pointer: tuple[dict, float],
) -> None:
    pointer, moment = resumed_pointer
    pointer["pid"] = int(Path("/proc/sys/kernel/pid_max").read_text()) + 1
    pointer["pid_start_time"] = None
    resumed = Path(pointer["log_path"]).with_name("resume-1.jsonl")
    stale = moment - QUIET_SECONDS
    os.utime(resumed, (stale, stale))
    row = _reading(pointer, moment)

    print(
        f"supervisor_alive={row['process_alive']} "
        f"classification={row['classification']} state={row['fleet_verdict']['state']}"
    )
    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["fleet_verdict"]["state"] in PANE_STATES["help"], row["fleet_verdict"]
