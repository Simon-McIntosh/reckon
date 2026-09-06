"""A finished run must never keep reading as working, nor a live one as dead.

Two mechanisms compound to make a finished run read as working. First,
classify_pointer's staleness heuristic discarded a manifest whenever the
process looked alive and the log file was newer — right for a manifest
superseded mid-run, wrong for a terminal one, since complete/blocked/failed
are facts a later log line cannot undo. Second, the ticker's stall check only
fired from the "working" state, so a run that died before its phase ever left
"starting" was permanently exempt from ever being marked stalled.

Liveness is the authority over terminal-looking reports in both directions: a
live process defers a complete, blocked or failed manifest verdict (the run
reads running until the process exits), and a run is only ever called
abandoned when the process table proves the worker is gone.
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


@pytest.mark.parametrize("status", ["complete", "failed"])
def test_terminal_manifest_survives_an_alive_process_and_a_newer_log(
    tmp_path, status
) -> None:
    """A verdict on disk outlives a later, unrelated log line while a live
    process defers the outcome: the manifest is never discarded and its raw
    spelling is preserved, the run reads running until the process exits, and
    only then does the manifest's verdict apply."""
    record, log, manifest = _record(
        tmp_path=tmp_path, phase="working", process_alive=True
    )
    now = time.time()
    _touch(
        manifest,
        text=f"node: the-node\nstatus: {status}\ncommits: HEAD\n",
        mtime=now - 10,
    )
    _touch(log, text='{"type":"turn.started"}\n', mtime=now)

    row = recovery.classify_pointer(record, now_seconds=now + 1)

    assert row["manifest_present"] is True
    assert row["manifest_reported_status"] == status
    assert row["classification"] == "running"

    record["process_alive"] = False
    dead = recovery.classify_pointer(record, now_seconds=now + 1)
    assert dead["classification"] == (
        "completed_unpromoted" if status == "complete" else "failed"
    )
    assert dead["manifest_status"] == status


def test_blocked_manifest_is_deferred_by_a_resumed_workers_newer_log(
    tmp_path,
) -> None:
    """Blocked is a solicitation, not a verdict: a resumed worker whose process
    is alive again defers it — the run reads running, not blocked, with the
    manifest kept so the verdict applies when the process exits."""
    record, log, manifest = _record(
        tmp_path=tmp_path, phase="working", process_alive=True
    )
    now = time.time()
    _touch(
        manifest,
        text="node: the-node\nstatus: blocked\ncommits: HEAD\n",
        mtime=now - 10,
    )
    _touch(log, text='{"type":"turn.started"}\n', mtime=now)

    row = recovery.classify_pointer(record, now_seconds=now + 1)

    assert row["manifest_present"] is True
    assert row["classification"] == "running"
    assert row["manifest_status"] is None


def test_blocked_manifest_survives_a_dead_process(tmp_path) -> None:
    """A newer log line from a dead process is not a resume, so the
    solicitation still stands."""
    record, log, manifest = _record(
        tmp_path=tmp_path, phase="working", process_alive=False
    )
    now = time.time()
    _touch(
        manifest,
        text="node: the-node\nstatus: blocked\ncommits: HEAD\n",
        mtime=now - 10,
    )
    _touch(log, text='{"type":"turn.started"}\n', mtime=now)

    row = recovery.classify_pointer(record, now_seconds=now + 1)

    assert row["manifest_present"] is True
    assert row["manifest_status"] == "blocked"
    assert row["classification"] == "blocked"


def test_blocked_manifest_is_deferred_until_the_process_exits(tmp_path) -> None:
    """A live process with no activity since the manifest was written has not
    answered the solicitation yet, so the run reads running; the blocked
    verdict stands once the process is gone."""
    record, log, manifest = _record(
        tmp_path=tmp_path, phase="working", process_alive=True
    )
    now = time.time()
    _touch(log, text='{"type":"turn.started"}\n', mtime=now - 10)
    _touch(manifest, text="node: the-node\nstatus: blocked\ncommits: HEAD\n", mtime=now)

    row = recovery.classify_pointer(record, now_seconds=now + 1)

    assert row["manifest_present"] is True
    assert row["classification"] == "running"

    record["process_alive"] = False
    dead = recovery.classify_pointer(record, now_seconds=now + 1)
    assert dead["classification"] == "blocked"
    assert dead["manifest_status"] == "blocked"


def test_manifest_reporting_terminal_is_read_before_the_phase_ever_advances(
    tmp_path,
) -> None:
    """A run killed right after dispatch is still read from its manifest."""
    record, _log, manifest = _record(
        tmp_path=tmp_path, phase="starting", process_alive=False
    )
    now = time.time()
    _touch(
        manifest, text="node: the-node\nstatus: complete\ncommits: HEAD\n", mtime=now
    )

    row = recovery.classify_pointer(record, now_seconds=now)

    assert row["classification"] == "completed_unpromoted"

    snapshot = recovery._watch_snapshot(record, moment=now, stall_seconds=600)
    assert snapshot["state"] == "complete"


def test_dispatched_with_no_manifest_and_a_dead_process_resolves_to_abandoned(
    tmp_path,
) -> None:
    """A run whose process is gone has stopped working whatever phase label it
    held, so it resolves to abandoned rather than stalled."""
    record, log, _manifest = _record(
        tmp_path=tmp_path, phase="starting", process_alive=False
    )
    started = time.time()
    _touch(log, text='{"type":"turn.started"}\n', mtime=started)

    snapshot = recovery._watch_snapshot(record, moment=started + 700, stall_seconds=600)
    assert snapshot["state"] == "abandoned"
    assert "quiet" not in snapshot["detail"]


def test_dispatched_alive_process_quiet_past_window_stalls(tmp_path) -> None:
    """A run only stalls when its process is still alive but its stream has been
    quiet past the window — a dead process abandons instead."""
    record, log, _manifest = _record(
        tmp_path=tmp_path, phase="starting", process_alive=True
    )
    started = time.time()
    _touch(log, text='{"type":"turn.started"}\n', mtime=started)

    baseline = recovery._watch_snapshot(record, moment=started + 100, stall_seconds=600)
    assert baseline["state"] == "dispatched"

    stalled = recovery._watch_snapshot(record, moment=started + 700, stall_seconds=600)
    assert stalled["state"] == "stalled"
    assert "quiet" in stalled["detail"]


def test_non_terminal_manifest_status_is_still_superseded_by_newer_activity(
    tmp_path,
) -> None:
    """The staleness heuristic is narrowed to non-terminal statuses, not removed."""
    record, log, manifest = _record(
        tmp_path=tmp_path, phase="working", process_alive=True
    )
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
