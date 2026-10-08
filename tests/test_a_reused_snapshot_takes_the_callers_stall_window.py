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

One further case holds a live child under the worker, the shape whose window
widens past the caller's to the run's own declared budget. It reads the run
past a short window and inside that budget and asserts the reused snapshot
keeps the widened verdict, because the budget the snapshot carries is what a
producer that reuses it recomputes the window from.

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

from reckon.crew import recovery_classification
from reckon.crew import recovery_watch
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


def _seed_live_child(run_id: str) -> dict:
    """Seed a live worker holding a live child, with a declared budget.

    The child is what widens the run's stall window from the caller's to the
    run's own declared allowance; the budget is declared far above the short
    caller window, so a read past the short window and inside the budget is the
    one that widening decides.
    """
    record = _seed(run_id)
    record["pid"] = os.getpid()
    record["attempt_budget_seconds"] = LONG_WINDOW
    runs.pointer_path(run_id).write_text(json.dumps(record))
    return record


@pytest.fixture
def live_child_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    recovery._SNAPSHOT_CACHE.clear()
    # Substitute the process table for the two liveness readings the stall
    # window widens on, so the case exercises the window a live child earns and
    # the reuse that must carry its budget, not this host's process table.
    (monkeypatch.setattr(recovery_classification, "local_liveness", lambda record: (True, True)), monkeypatch.setattr(recovery_watch, "local_liveness", lambda record: (True, True)))
    (monkeypatch.setattr(recovery_classification, "_live_descendant", lambda pid: True), monkeypatch.setattr(recovery_watch, "_live_descendant", lambda pid: True))
    return _seed_live_child("r-live-child-budget")


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


def test_a_live_child_keeps_the_budget_window_on_reuse(
    live_child_record: dict,
) -> None:
    """A reused snapshot keeps the widest window a live child earns.

    A live child under the worker widens the run's window to its own declared
    budget, so the storing read — quiet past the caller's short window but
    inside the budget — is working and the stored window is the budget. A
    reused snapshot recomputes the window the same way for either caller, so
    its state must equal a full recompute's and not the short window's stall:
    a snapshot that carried no budget would widen to nothing, judge the run by
    ``SHORT_WINDOW`` and read stalled.
    """
    first = _reused(live_child_record, stall_seconds=SHORT_WINDOW)
    assert first["state"] == "working", first["state"]
    assert first["process_descendant_alive"] is True, first
    assert first["stall_window_seconds"] == LONG_WINDOW, first["stall_window_seconds"]

    again = _reused(live_child_record, stall_seconds=SHORT_WINDOW)

    assert again["state"] == _full_state(
        live_child_record, stall_seconds=SHORT_WINDOW
    )
    assert again["state"] == "working", again["state"]
    assert again["stall_window_seconds"] == LONG_WINDOW, again["stall_window_seconds"]
