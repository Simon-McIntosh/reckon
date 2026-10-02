"""A reused snapshot is judged against its caller's stall window.

The snapshot reuse key is the classification's own input composition, and the
stall window a caller passes is deliberately not part of it: the window is a
reading parameter rather than an input file, so two producers watching one
unchanged run with different windows reach the same cached entry. The window a
served snapshot recomputes the silence against must therefore be the caller's
own, not the one frozen onto the entry when it was first classified. A reused
snapshot's state for a given window must equal a full recompute's for that same
window; otherwise one caller's verdict leaks into the other's.

Each case reads one unchanged run through the shared cache twice with two
different windows, in both orders, and asserts each read equals the full
recompute for its own window.

The negative control recomputes the window from the frozen snapshot field
rather than from the caller's ``stall_seconds``; the long-window read then
reports the short window's verdict and the case fails.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

PROJECT = "reuse-window"
FOREIGN_HOST = "a-login-node-that-is-not-this-one"
MOMENT = 1_800_000_000.0
QUIET_SECONDS = 600
SHORT_WINDOW = 60
LONG_WINDOW = 3600


def _pointer(run_id: str, directory: Path) -> dict:
    """A live, working run, quiet longer than the short window but not the long."""
    started = datetime.fromtimestamp(MOMENT - QUIET_SECONDS, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": f"node-{run_id}", "plan": "plan-a"},
        "phase": "working",
        # A foreign-launching host keeps the stored liveness reading rather than
        # probing this host's process table, so the fixture needs no live pid.
        "launcher_host": FOREIGN_HOST,
        "process_alive": True,
        "backend": "claude",
        "launch": "cli",
        "command": "claude",
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "status": "running",
        "attempt_started_at": started,
    }


def _seed(run_id: str) -> dict:
    """Write one unchanged run: a live working pointer, a running manifest, a quiet stream."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    record = _pointer(run_id, directory)
    Path(record["manifest_path"]).write_text(
        f"---\nnode: {run_id}\nstatus: running\n---\n\nbody\n", encoding="utf-8"
    )
    # An empty stream carries no complete record, so the run is not read as a
    # death and the stall reading governs the verdict.
    stream = Path(record["log_path"])
    stream.write_text("", encoding="utf-8")
    os.utime(stream, (MOMENT - QUIET_SECONDS, MOMENT - QUIET_SECONDS))
    pointer = runs.pointer_path(run_id)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(record))
    return record


@pytest.fixture
def run_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    recovery._SNAPSHOT_CACHE.clear()
    return _seed("r-window")


def _reused(record: dict, *, stall_seconds: int) -> dict:
    """Read through the shared module-level cache, as a producer passing no cache does."""
    return recovery._watch_snapshot(record, moment=MOMENT, stall_seconds=stall_seconds)


def _full_state(record: dict, *, stall_seconds: int) -> str:
    """The state a full recompute yields for one window, off the reuse cache."""
    snapshot = recovery._compute_watch_snapshot(
        record, moment=MOMENT, stall_seconds=stall_seconds
    )
    return snapshot["state"]


def test_short_then_long_window_matches_the_full_recompute(run_record: dict) -> None:
    """A short-window entry served to a long-window caller is judged long."""
    short = _reused(run_record, stall_seconds=SHORT_WINDOW)
    assert short["state"] == "stalled"
    assert short["stall_window_seconds"] == SHORT_WINDOW

    # The same unchanged run, read again through the shared cache with the long
    # window: the entry is reused, and the verdict must move with the window.
    long = _reused(run_record, stall_seconds=LONG_WINDOW)

    assert long["state"] == _full_state(run_record, stall_seconds=LONG_WINDOW)
    assert long["state"] == "working", long["state"]
    assert long["stall_window_seconds"] == LONG_WINDOW


def test_long_then_short_window_matches_the_full_recompute(run_record: dict) -> None:
    """A long-window entry served to a short-window caller is judged short."""
    long = _reused(run_record, stall_seconds=LONG_WINDOW)
    assert long["state"] == "working"
    assert long["stall_window_seconds"] == LONG_WINDOW

    short = _reused(run_record, stall_seconds=SHORT_WINDOW)

    assert short["state"] == _full_state(run_record, stall_seconds=SHORT_WINDOW)
    assert short["state"] == "stalled", short["state"]
    assert short["stall_window_seconds"] == SHORT_WINDOW
