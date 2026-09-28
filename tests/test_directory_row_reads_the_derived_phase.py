"""The directory row reports the phase the classifier and live rows derive.

A run's stored ``phase`` is the launcher's label, and only ``observe`` advances
it, so a worker thinking and editing for an hour still carries the ``starting``
label its launch wrote. The classifier and the live view derive the row's phase
from the run's own newest stream instead. The directory row is what the CLI
verb and the MCP directory view both render, so a row that copied the stored
field made one run read as ``working`` in one surface and ``starting`` in
another. Each case leaves the pointer's own label at ``starting``, so the word
the row reports cannot have come from it.

Every case holds a real process for the pointer's pid, because liveness is what
makes the run's stored label readable at all: an ended run is a different
question, and the derivation is only asked while the worker lives.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import mcp
from reckon.cli import main as cli_main
from reckon.crew import recovery, runs

HOST = socket.gethostname()

# The label a launcher writes before the worker's first turn is under way.
STORED_PHASE = "starting"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point every crew directory at a temporary configuration home, and prove it."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert runs.crew_home().is_relative_to(tmp_path)


def _write_stream(path: Path, records: Sequence[dict[str, Any]]) -> Path:
    """Write one stream of records, in the order an engine writes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _start_worker() -> subprocess.Popen:
    """A real process for the pointer's pid, so liveness is the table's word."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _survivors(pids: Sequence[int], *, grace: float = 5.0) -> list[int]:
    """Which of these pids still runs after a bounded grace."""
    deadline = time.monotonic() + grace
    while True:
        live = [pid for pid in pids if runs.process_alive(pid) is True]
        if not live or time.monotonic() >= deadline:
            return live
        time.sleep(0.02)


def _end_worker(worker: subprocess.Popen) -> None:
    """End the process a case started and fail if any part of it survives."""
    worker.terminate()
    worker.wait()
    stragglers = _survivors([worker.pid])
    assert not stragglers, f"processes this case started survived it: {stragglers}"


def _pointer(
    run_id: str,
    *,
    pid: int,
    stream: Path,
    phase: str = STORED_PHASE,
) -> dict[str, Any]:
    """One live pointer on the reading host, still at its launcher's label."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: in-progress\ncommits:\nblockers:\n",
        encoding="utf-8",
    )
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": "directory-phase-fixture",
        "session": "phase-coordinator",
        "repo": "/repos/directory-phase-fixture",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "1h"},
        "phase": phase,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "worker.stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid),
        "launcher_host": HOST,
        "worktree": str(directory / "tree"),
        "base_sha": "",
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _row_from_result(result: dict[str, Any], run_id: str) -> dict[str, Any]:
    """One run's row out of a directory read, wherever the read placed it."""
    rows = [
        row
        for coordinator in result.get("coordinators", [])
        for row in coordinator.get("runs", [])
    ] + list(result.get("unowned_runs", []))
    matches = [row for row in rows if row.get("run_id") == run_id]
    assert len(matches) == 1, matches
    return matches[0]


def _directory_row(run_id: str) -> dict[str, Any]:
    """The row the MCP directory view renders for one run."""
    return _row_from_result(mcp._crew(view="directory"), run_id)


def test_an_assistant_record_makes_the_directory_row_read_working(
    tmp_path: Path,
) -> None:
    """The row reports the phase the run's stream supports, not the pointer's label.

    The pointer is written as a launch leaves it — the pre-spawn label, never
    folded by ``observe`` — and the stream holds the worker's first assistant
    turn. The stored label and the row's phase are asserted together: the label
    is what the row would report if it copied the pointer, so a case that read
    only the row could pass on a pointer that had advanced by itself.
    """
    run_id = "r-directory-assistant"
    stream = _write_stream(
        runs.run_dir(run_id) / "stream.jsonl",
        [{"type": "system"}, {"type": "assistant"}],
    )
    worker = _start_worker()
    try:
        pointer = _pointer(run_id, pid=worker.pid, stream=stream)
        row = _directory_row(run_id)
        cli_row = _row_from_result(
            json.loads(
                CliRunner()
                .invoke(cli_main, ["crew", "directory", "--run", run_id])
                .output
            ),
            run_id,
        )
        assert runs.read_pointer(run_id)["phase"] == STORED_PHASE
        classified = recovery.classify_pointer(pointer)
    finally:
        _end_worker(worker)

    assert row["phase"] == "working", row
    assert cli_row["phase"] == "working", cli_row
    assert classified["phase"] == "working", classified["phase"]
    assert classified["stored_phase"] == STORED_PHASE, classified["stored_phase"]


def test_a_system_record_only_keeps_the_stored_label_in_the_directory_row(
    tmp_path: Path,
) -> None:
    """No assistant turn means no advance: the launcher's label stands.

    The negative reading beside the case above. A stream that exists is not
    evidence of work — an engine opens one and writes its opening records before
    the model has answered anything — so the row keeps the label its launcher
    set rather than inventing an advance the run's own record does not show.
    """
    run_id = "r-directory-system-only"
    stream = _write_stream(
        runs.run_dir(run_id) / "stream.jsonl",
        [{"type": "system"}, {"type": "turn.started"}],
    )
    worker = _start_worker()
    try:
        pointer = _pointer(run_id, pid=worker.pid, stream=stream)
        row = _directory_row(run_id)
        classified = recovery.classify_pointer(pointer)
    finally:
        _end_worker(worker)

    assert row["phase"] == STORED_PHASE, row
    assert classified["phase"] == STORED_PHASE, classified["phase"]
