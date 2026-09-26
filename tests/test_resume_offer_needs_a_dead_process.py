"""A resume offer for a parked run waits until the worker's process has ended.

A run parked on an external condition is resumed when the condition reports
terminal, and the row that says so is what a coordinator acts on. The condition
is only half the question. A worker whose process is still running is still
writing the run, and a resume on top of it starts a second worker on the same
run; a worker whose liveness nothing established is not a death either, so
offering the resume would rest it on a reading nobody took. Measured 2026-09-22:
a coordinator read ``ready to resume`` for a run whose process was alive
(plan §1), which is the reading this file pins.

Every row here is driven through the classifier and the published watch line:
``classify_pointer`` composes the clause, ``_watch_snapshot`` reduces the
pointer, ``_watch_transition`` builds the event the events log persists, and
``format_watch_transition`` renders it through the ticker's own reason clause.
The clause is asserted on the row the classifier composed and on the rendered
line, because the pane is where a coordinator reads it.

The condition is a file the run waits for, so the fixture proves the wait is
met by its own path rather than by a probe stub: each case asserts the row into
the arm under test (``wait_condition_state == "met"``) before asserting what
the arm says, so a case that stopped reaching that arm fails rather than passes
vacuously. The run ids are deliberately neutral — the row prints the run id in
its node cell, so an id spelling one of the words under test would satisfy that
case's own assertion from the node name.

The declared mutation removes the liveness gate from the wait-met arm; the live
case's row must then contain ``resume`` again and its first assertion fails.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import ticker as ticker_module
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "remove the liveness gate from the wait-met arm; the live case's row must "
    "contain resume again and the assertion fails"
)

# The condition the parked runs declare, printed in the row's reason so the
# pane can be asserted to carry the condition it is waiting on.
CONDITION = "scheduler job 42 has finished"

# The pane these rows render at: the widened grid the workstation measures,
# where the reason clause keeps its whole width.
PANE_WIDTH = 208

# The window these runs are judged against, in seconds. The rows are not
# stalls, so it only fixes the age the wait is read against.
STALL_SECONDS = 900

LIVE_RUN_ID = "r-parked-held"
GONE_RUN_ID = "r-parked-vacant"
UNLOGGED_RUN_ID = "r-parked-unlogged"


def _home_fingerprint(home: Path) -> list[tuple[str, int]]:
    """The real config home's own entries, by name and mtime.

    One directory level only: the point is to catch a write that landed in the
    reader's own home, and a recursive walk of a live fleet's home on GPFS is
    the crawl this check must not itself become.
    """
    if not home.is_dir():
        return []
    return sorted((entry.name, entry.stat().st_mtime_ns) for entry in home.iterdir())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against a temporary config home, and prove the real one untouched.

    The fixture is the receipt for its own isolation: a pointer or a run
    directory written to the real home would both escape the test and collide
    with a live fleet, so the fingerprint is taken before the environment moves
    and the same home is re-read after the case ends.
    """
    real_home = Path(os.path.expanduser("~")) / ".config" / "reckon"
    before = _home_fingerprint(real_home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert _home_fingerprint(real_home) == before


def _waiting_manifest(run_id: str, awaited: Path) -> str:
    return (
        f"node: {run_id}\n"
        "status: waiting\n"
        f"wait_condition: {CONDITION}\n"
        f"wait_file: {json.dumps([str(awaited)])}\n"
        "resume_brief: read the job's output and continue\n"
    )


def _parked_pointer(
    tmp_path: Path, run_id: str, *, pid: int | None
) -> tuple[dict, float, Path]:
    """One run parked on a condition that has already reported terminal.

    The awaited path exists before the row is read, so the condition is met by
    the fixture's own file rather than by a stubbed probe, and the three cases
    differ only in what the process table says about the worker.
    """
    awaited = tmp_path / f"{run_id}-condition-met"
    awaited.write_text("done\n", encoding="utf-8")
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=pid,
        phase="working",
        manifest_body=_waiting_manifest(run_id, awaited),
        write_stream=False,
    )
    moment = time.time()
    return pointer, moment, awaited


def _render_parked_row(pointer: dict, moment: float) -> tuple[dict, dict, str]:
    """The classifier's reading and the watch line a coordinator sees."""
    row = recovery.classify_pointer(
        pointer, now_seconds=moment, stale_after_seconds=STALL_SECONDS
    )
    snapshot = recovery._watch_snapshot(
        pointer, moment=moment, stall_seconds=STALL_SECONDS
    )
    transition = recovery._watch_transition(
        "resume-offer-fixture",
        kind="stalled",
        snapshot=snapshot,
        previous="working",
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({str(snapshot["run_id"]): snapshot}),
    )
    line = recovery.format_watch_transition(
        transition, ticker=ticker_module.Ticker(width=PANE_WIDTH, color=False)
    )
    return row, snapshot, line


def test_a_met_wait_beside_a_live_process_waits(tmp_path: Path) -> None:
    """The live worker: the condition is met and the run is still being written.

    A resume here starts a second worker on a run the first one has not left,
    so the row waits on the process instead of offering the resume, and names
    the reading it is holding.
    """
    with liveness._live_child() as pid:
        pointer, moment, awaited = _parked_pointer(tmp_path, LIVE_RUN_ID, pid=pid)
        row, snapshot, line = _render_parked_row(pointer, moment)
        reason = row["fleet_verdict"]["detail"]

    assert awaited.is_file(), awaited
    assert row["wait_condition_state"] == "met", row["wait_condition_state"]
    assert row["process_alive"] is True, row["process_alive"]
    assert snapshot["process_alive"] is True, snapshot["process_alive"]
    assert "resume" not in reason, reason
    assert "waiting" in reason, reason
    assert "resume" not in line, line
    assert CONDITION in line, line
    assert row["recovery_classification"] == "waiting", row["recovery_classification"]
    assert row["recovery"] == "wait", row["recovery"]
    assert snapshot["state"] == "waiting", snapshot["state"]


def test_a_met_wait_beside_a_gone_process_offers_the_resume(tmp_path: Path) -> None:
    """The side that must not be lost: nothing holds the run, so it resumes.

    The pid is a real number far above the kernel's ceiling, so the process
    table was asked on this host and answered that nothing holds it — the
    observation that licenses the offer.
    """
    pointer, moment, awaited = _parked_pointer(
        tmp_path, GONE_RUN_ID, pid=liveness._absent_pid()
    )
    row, snapshot, line = _render_parked_row(pointer, moment)

    assert awaited.is_file(), awaited
    assert row["wait_condition_state"] == "met", row["wait_condition_state"]
    assert row["process_alive"] is False, row["process_alive"]
    assert row["liveness_proven"] is True, row["liveness_proven"]
    assert "ready to resume" in row["fleet_verdict"]["detail"], row["fleet_verdict"]
    assert "ready to resume" in line, line
    assert row["recovery_classification"] == "ready", row["recovery_classification"]
    assert row["recovery"] == "resume", row["recovery"]
    assert snapshot["state"] == "waiting", snapshot["state"]


def test_a_met_wait_beside_an_unproven_process_waits(tmp_path: Path) -> None:
    """No pid was recorded, so nothing observed the process either way.

    An unproven reading is not a death: the offer would rest on an observation
    nobody took, which is what a coordinator acting on it pays for. The row
    gets the wait reading and names ``liveness unknown``, so a reader sees why
    the offer is withheld rather than concluding the run was resumed.
    """
    pointer, moment, awaited = _parked_pointer(tmp_path, UNLOGGED_RUN_ID, pid=None)
    row, snapshot, line = _render_parked_row(pointer, moment)
    reason = row["fleet_verdict"]["detail"]

    assert awaited.is_file(), awaited
    assert row["wait_condition_state"] == "met", row["wait_condition_state"]
    assert row["process_alive"] is None, row["process_alive"]
    assert row["liveness_proven"] is False, row["liveness_proven"]
    assert "liveness unknown" in reason, reason
    assert "resume" not in reason, reason
    assert "waiting" in reason, reason
    assert "resume" not in line, line
    assert CONDITION in line, line
    assert row["recovery_classification"] == "waiting", row["recovery_classification"]
    assert row["recovery"] == "wait", row["recovery"]
    assert snapshot["state"] == "waiting", snapshot["state"]
