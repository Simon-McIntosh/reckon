"""A dead worker is a row on the snapshot that observes it.

A non-terminal manifest is the worker's own last word, and it is read as one
while the process lives. The moment the process is gone that word is stale: a
run killed mid-turn stayed in the working column until its stream had been
quiet for the whole stall window, so a pane reported a dead process as working
for fifteen minutes. The stream says which end this was — a last record of
result is a turn that ran to its own conclusion, any other last record is a
death mid-turn — and it is read on the snapshot that sees the process table
change, so the row appears at the death rather than at the end of the window.

Where the supervisor recorded the worker's exit, the run already reads as the
interrupted one it is; these cases cover the deaths nothing recorded, which is
what a killed process leaves behind.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from reckon import crew
from reckon.crew import recovery, runs

STALL_SECONDS = recovery.parse_duration(recovery.DEFAULT_WATCH_STALL_WINDOW)
# The phrase only the death reading composes: it is the stream's tail being
# reported, which no other reader of this run states.
STREAM_TAIL_CLAUSE = "the stream's last record is"


def _start_worker() -> subprocess.Popen:
    """A real process to kill, so liveness is the process table's answer."""
    return subprocess.Popen(["sleep", "120"])


def _kill(worker: subprocess.Popen) -> None:
    worker.kill()
    worker.wait()


def _run_directory(run_id: str) -> Path:
    directory = Path(crew.run_dir(run_id))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _manifest(directory: Path) -> Path:
    path = directory / "manifest.md"
    path.write_text(
        "node: the-worker\nstatus: in-progress\ncommits:\nblockers:\n",
        encoding="utf-8",
    )
    return path


def _stream(directory: Path, *, last_record_type: str) -> Path:
    """The run's stream, ending in the record type the case is about."""
    path = directory / "stream.jsonl"
    records = [
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "assistant", "message": {"content": []}},
    ]
    if last_record_type != "assistant":
        records.append({"type": last_record_type})
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _exit_record(
    directory: Path,
    *,
    run_id: str,
    pid: int,
    signal_number: int | None = None,
    exit_code: int | None = None,
) -> Path:
    """The record the supervisor writes as the worker's wait is collected."""
    path = directory / recovery.EXIT_RECORD_NAME
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "recorded_by": "supervisor",
                "worker_pid": pid,
                "launched_at": "2026-01-01T00:00:00+00:00",
                "exited_at": "2026-01-01T00:10:00+00:00",
                "stream_records_seen": 3,
                "last_record_type": "assistant",
                "ended_during": "working",
                "exit_code": exit_code,
                "signal": signal_number,
                "signal_name": "SIGKILL" if signal_number else None,
            }
        ),
        encoding="utf-8",
    )
    return path


def _pointer(
    directory: Path,
    *,
    run_id: str,
    pid: int,
) -> dict:
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": "the-worker", "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "pid": pid,
        "pid_start_time": runs._process_start_time(pid),
        "launcher_host": socket.gethostname(),
        "process_alive": None,
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "worktree": str(directory / "worktree"),
        "base_sha": "0" * 40,
    }


def _write_pointer(run_id: str, pointer: dict) -> None:
    crew._write_json(crew.pointer_path(run_id), pointer)


def _transitions(stream_path: Path, run_id: str) -> list[dict]:
    return [
        event
        for event in runs.read_stream_events(stream_path)
        if event.get("run_id") == run_id and event.get("event") == "transition"
    ]


def test_a_killed_worker_is_reported_on_the_snapshot_that_sees_it_die(
    isolated_reckon_home: Path,
) -> None:
    """The death is the news, and it is reported when it happens.

    The run is built alive so the publisher records a baseline of it working;
    the kill then falls between two snapshots, which is the only arrangement in
    which a transition can be asked for at all.
    """
    run_id = "r-killed-mid-turn"
    directory = _run_directory(run_id)
    worker = _start_worker()
    _stream(directory, last_record_type="assistant")
    _manifest(directory)
    pointer = _pointer(directory, run_id=run_id, pid=worker.pid)
    _write_pointer(run_id, pointer)

    try:
        with runs._project_watch_claim("proj", "1h") as (acquired, registration):
            assert acquired is True
            stream_path = Path(registration["stream_path"])
            baseline = [
                event
                for event in runs.read_stream_events(stream_path)
                if event.get("run_id") == run_id
            ]
            assert [event["event"] for event in baseline] == ["baseline"]
            assert baseline[0]["to_state"] == "working"

            _kill(worker)
            assert not (directory / recovery.EXIT_RECORD_NAME).exists()

            # A snapshot's worth of elapsed time, far short of the window.
            quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=time.time())
            assert quiet < STALL_SECONDS

            crew.list_live(project="proj")
            killed = _transitions(stream_path, run_id)
    finally:
        if worker.poll() is None:
            _kill(worker)

    assert len(killed) == 1
    row = killed[0]
    assert row["from_state"] == "working"
    assert row["to_state"] == "blocked"
    assert row["recovery_classification"] == recovery.INTERRUPTED_RUN_PHASE
    assert row["recovery"] == "redispatch"
    # The reason says the process exited, names the silence the exit left, and
    # names the record the death interrupted — a reader choosing a recovery
    # needs both facts on the row.
    assert "no recorded exit" in row["detail"]
    assert "before the run completed" in row["detail"]
    assert STREAM_TAIL_CLAUSE in row["detail"]
    assert "assistant" in row["detail"]
    # It is not the stall row: the window has not elapsed and the row does not
    # claim it has.
    assert "stream quiet for" not in row["detail"]


def test_a_stream_that_ended_in_a_result_record_is_not_a_death(
    isolated_reckon_home: Path,
) -> None:
    """A turn that ended on its own is a different reading, and a different fix.

    The run is otherwise identical to the killed one above — same manifest, same
    dead pid, same silence — and only the stream's last record differs, so the
    death reading is exactly what this case forbids. The assistant arm is read
    immediately afterwards on the same run, because an absence claim on its own
    would also pass for a reader that reports no death at all.
    """
    run_id = "r-ended-turn"
    directory = _run_directory(run_id)
    worker = _start_worker()
    _stream(directory, last_record_type="result")
    _manifest(directory)
    pointer = _pointer(directory, run_id=run_id, pid=worker.pid)
    _write_pointer(run_id, pointer)

    try:
        _kill(worker)
        ended = recovery._watch_snapshot(
            pointer, moment=time.time(), stall_seconds=STALL_SECONDS
        )

        _stream(directory, last_record_type="assistant")
        interrupted = recovery._watch_snapshot(
            pointer, moment=time.time(), stall_seconds=STALL_SECONDS
        )

        with runs._project_watch_claim("proj", "1h") as (acquired, registration):
            assert acquired is True
            stream_path = Path(registration["stream_path"])
            crew.list_live(project="proj")
            published = _transitions(stream_path, run_id)
    finally:
        if worker.poll() is None:
            _kill(worker)

    assert STREAM_TAIL_CLAUSE not in str(ended.get("detail") or "")
    # Both ends are stops, so the compatibility lifecycle state groups them as
    # blocked; the reading that separates a concluded turn from a mid-turn
    # death is the typed recovery classification, which is what a reader acts
    # on and what the ticker's state cell spells.
    assert ended.get("recovery_classification") == "ended-without-manifest"
    assert interrupted.get("recovery_classification") == recovery.INTERRUPTED_RUN_PHASE
    assert ended.get("recovery") == "resume"
    assert interrupted.get("recovery") == "redispatch"
    assert STREAM_TAIL_CLAUSE in str(interrupted.get("detail") or "")
    assert all(
        STREAM_TAIL_CLAUSE not in str(event.get("detail") or "") for event in published
    )


def test_a_recorded_clean_exit_is_named_rather_than_read_as_silence(
    isolated_reckon_home: Path,
) -> None:
    """A worker that exited without finishing its turn is a death with a record.

    A nonzero code is not a signal, so no other reader takes this run for an
    interruption, and the end the supervisor collected is the fact the row has
    to carry — "gone with no recorded exit" would be wrong here in the one
    direction that matters, since the exit is exactly what was recorded.
    """
    run_id = "r-exited-mid-turn"
    directory = _run_directory(run_id)
    worker = _start_worker()
    _stream(directory, last_record_type="assistant")
    _manifest(directory)
    pointer = _pointer(directory, run_id=run_id, pid=worker.pid)
    _write_pointer(run_id, pointer)

    try:
        _kill(worker)
        _exit_record(directory, run_id=run_id, pid=worker.pid, exit_code=3)

        snapshot = recovery._watch_snapshot(
            pointer, moment=time.time(), stall_seconds=STALL_SECONDS
        )
    finally:
        if worker.poll() is None:
            _kill(worker)

    assert snapshot["state"] == "blocked"
    assert "exited with code 3" in str(snapshot.get("detail") or "")
    assert "no recorded exit" not in str(snapshot.get("detail") or "")
    assert STREAM_TAIL_CLAUSE in str(snapshot.get("detail") or "")
