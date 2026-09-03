"""A finished run must never keep reading as working.

Two mechanisms compound to produce that failure. First, classify_pointer's
staleness heuristic discarded a manifest whenever the process looked alive and
the log file was newer — right for a manifest superseded mid-run, wrong for a
terminal one, since complete/blocked/failed are facts a later log line cannot
undo. Second, the ticker's stall check only fired from the "working" state, so
a run that died before its phase ever left "starting" was permanently exempt
from ever being marked stalled.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from reckon.crew import recovery


def _touch(path: Path, *, text: str, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _record(*, tmp_path: Path, phase: str, process_alive: bool | None, **overrides):
    log = tmp_path / "log.jsonl"
    manifest = tmp_path / "manifest.md"
    record = {
        "run_id": "r-node",
        "project": "proj",
        "node": {"id": "the-node", "plan": "plan-a", "time_budget": "20m"},
        "phase": phase,
        "created_at": "2026-09-03T00:00:00+00:00",
        "manifest_path": str(manifest),
        "log_path": str(log),
        "process_alive": process_alive,
    }
    record.update(overrides)
    return record, log, manifest


@pytest.mark.parametrize("status", ["complete", "blocked", "failed"])
def test_terminal_manifest_survives_an_alive_process_and_a_newer_log(
    tmp_path, status
) -> None:
    """A verdict on disk outlives a later, unrelated log line."""
    record, log, manifest = _record(tmp_path=tmp_path, phase="working", process_alive=True)
    now = time.time()
    _touch(manifest, text=f"node: the-node\nstatus: {status}\ncommits: HEAD\n", mtime=now - 10)
    _touch(log, text='{"type":"turn.started"}\n', mtime=now)

    row = recovery.classify_pointer(record, now_seconds=now + 1)

    assert row["manifest_status"] == status
    assert row["manifest_present"] is True
    assert row["classification"] in {"completed_unpromoted", "blocked", "failed"}
    assert row["classification"] != "running"


def test_manifest_reporting_terminal_is_read_before_the_phase_ever_advances(
    tmp_path,
) -> None:
    """A run killed right after dispatch is still read from its manifest."""
    record, log, manifest = _record(
        tmp_path=tmp_path, phase="starting", process_alive=False
    )
    now = time.time()
    _touch(manifest, text="node: the-node\nstatus: complete\ncommits: HEAD\n", mtime=now)

    row = recovery.classify_pointer(record, now_seconds=now)

    assert row["classification"] == "completed_unpromoted"

    snapshot = recovery._watch_snapshot(record, moment=now, stall_seconds=600)
    assert snapshot["state"] == "complete"


def test_dispatched_with_no_manifest_and_no_activity_resolves_to_stalled(
    tmp_path,
) -> None:
    """A run that dies before its phase ever leaves "starting" can still stall."""
    record, log, manifest = _record(
        tmp_path=tmp_path, phase="starting", process_alive=False
    )
    started = time.time()
    _touch(log, text='{"type":"turn.started"}\n', mtime=started)

    baseline = recovery._watch_snapshot(record, moment=started + 100, stall_seconds=600)
    assert baseline["state"] == "dispatched"

    stalled = recovery._watch_snapshot(record, moment=started + 700, stall_seconds=600)
    assert stalled["state"] == "stalled"
    assert "quiet" in stalled["reason"]


def test_non_terminal_manifest_status_is_still_superseded_by_newer_activity(
    tmp_path,
) -> None:
    """The staleness heuristic is narrowed to non-terminal statuses, not removed."""
    record, log, manifest = _record(tmp_path=tmp_path, phase="working", process_alive=True)
    now = time.time()
    _touch(
        manifest,
        text="node: the-node\nstatus: in-progress\ncommits: HEAD\n",
        mtime=now - 10,
    )
    _touch(log, text='{"type":"turn.started"}\n', mtime=now)

    row = recovery.classify_pointer(record, now_seconds=now + 1)

    assert row["manifest_present"] is False
    assert row["manifest_status"] is None
