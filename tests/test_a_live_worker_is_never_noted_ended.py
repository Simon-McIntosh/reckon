"""Process-ended wording requires an observed end of the recorded worker."""

from __future__ import annotations

import json
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery_classification, recovery_liveness, runs


@pytest.mark.parametrize("state", ["alive", "unknown", "exited"])
def test_a_process_ended_note_requires_an_exited_worker(state: str, home: Path) -> None:
    child = subprocess.Popen(["sleep", "60"])
    try:
        start = recovery_liveness._process_start_time(child.pid)
        if state == "exited":
            child.terminate()
            child.wait(timeout=5)

        run_id = f"r-worker-{state}"
        directory = Path(runs.run_dir(run_id))
        directory.mkdir(parents=True)
        stream = directory / "stream.jsonl"
        stream.write_text('{"type":"assistant","message":{"content":[]}}\n')
        worker = directory / "worker.json"
        if state == "unknown":
            worker.write_text('{"pid":')
        else:
            worker.write_text(json.dumps({"pid": child.pid, "pid_start_time": start}))
        manifest = directory / "manifest.md"
        manifest.write_text("status: in-progress\ncheckpoint: starting work\n")
        pointer = {
            "run_id": run_id,
            "project": "worker-note-fixture",
            "node": {"id": run_id, "role": "implement", "time_budget": "40m"},
            "phase": "working",
            "created_at": datetime.now(UTC).isoformat(),
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "stderr_path": str(directory / "stderr.log"),
            "pid": None if state == "unknown" else child.pid,
            "pid_start_time": start,
            "process_alive": None,
            "launcher_host": socket.gethostname(),
        }
        row = recovery_classification.classify_pointer(pointer, now_seconds=time.time())

        expected_alive = {"alive": True, "unknown": None, "exited": False}[state]
        assert row["process_alive"] is expected_alive, row
        assert row["classification"] == "running", row
        detail = row["detail"]
        print(f"{state}: {detail}")
        if state == "exited":
            assert "process ended" in detail, detail
            assert "not reported dead" in detail, detail
        else:
            assert "process ended" not in detail, detail
            assert "not reported dead" not in detail, detail
        if state == "unknown":
            assert "unknown" in detail, detail
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
