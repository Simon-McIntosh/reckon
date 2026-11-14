"""A repaired status moves the run's phase with it.

``crew repair --status`` rewrites a manifest's verdict because the coordinator
read the run and decided what the record should say. The manifest is what the
classifier reads to say what a run produced, and the live pointer's phase is
what the live view reads to say where it stands. A repair that rewrote one and
left the other named a run two readers disagreed about: the view read the word
the repair replaced while the reflex read the verdict, and a coordinator was
offered the run's blocked reading beside a review dispatch for work already
delivered. Measured on imas-efit on 10 November, where the offered dispatch was
taken by hand thirty-nine seconds before the reflex dispatched the same review.

The repair moves the pointer's phase onto the verdict, so both readers answer
from one state. This check drives a temporary crew home through that sequence:
a blocked run is read through ``crew view live`` and through the reflex's own
pass, repaired to ``complete``, and read again through both. The two readings
must name the same run — neither "blocked", and the same recovery verb and the
same classification — and the live view must no longer offer the action a
blocked run gets.

The negative control is the repair without the pointer update, which is the
code as it stood: the manifest reads ``complete`` while the pointer still reads
``blocked``, and the first case fails on the live view's phase. The declared
mutation therefore removes the phase write from
:func:`reckon.crew.runs.repair_manifest_status`.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.mcp_views import serve_crew_view

HOST = socket.gethostname()
PROJECT = "repaired-status-fixture"
RUN = "r-repaired-status"
NODE = "a-repaired-status"

BLOCKED_MANIFEST = (
    f"node: {NODE}\nstatus: blocked\nblockers: the worker could not reach the lane\n"
)


# Placed beyond the kernel's largest allocatable pid, so the classifier reads a
# run whose process this host cannot look up rather than one it finds alive.
def _pid_beyond_the_table() -> int:
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 1


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A crew home of this fixture's own, so no real fleet directory is read."""
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _write_run(home: Path, *, phase: str, manifest: str) -> Path:
    """Write one blocked run's pointer and manifest into the fixture home."""
    run_directory = home / "crew" / "runs" / RUN
    run_directory.mkdir(parents=True, exist_ok=True)
    manifest_path = run_directory / "manifest.md"
    manifest_path.write_text(manifest, encoding="utf-8")
    stream = run_directory / "stream.jsonl"
    stream.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
    pointer = {
        "run_id": RUN,
        "project": PROJECT,
        "session": "s-coordinator",
        "node": {"id": NODE, "plan": "a-plan", "section": "s1"},
        "phase": phase,
        "attempt": 1,
        "created_at": (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat(),
        "manifest_path": str(manifest_path),
        "log_path": str(stream),
        "stderr_path": str(run_directory / "stderr.log"),
        "process_alive": None,
        "pid": _pid_beyond_the_table(),
        "launcher_host": HOST,
        "worktree": str(home.parent / "tree"),
        "base_sha": "",
    }
    pointer_path = runs.pointer_path(RUN)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    return manifest_path


def _live_row() -> dict:
    """The run's row as ``crew view live`` serves it."""
    view = serve_crew_view("live", project=PROJECT, session="s-coordinator")
    assert view.get("ok") is True, view
    rows = [row for row in view["runs"] if row.get("run_id") == RUN]
    assert len(rows) == 1, view["runs"]
    return rows[0]


def _reflex_row() -> dict:
    """The run's row as the review reflex's own pass classifies it."""
    report = recovery.recover(project=PROJECT)
    rows = [row for row in report["runs"] if row.get("run_id") == RUN]
    assert len(rows) == 1, report["runs"]
    return rows[0]


def test_a_repair_moves_the_phase_the_live_view_reads(crew_home: Path) -> None:
    """Both readers name one run after the repair, and neither names it blocked.

    The blocked reading is asserted first, so the check shows the state the
    repair replaced rather than assuming it. The negative control (the repair
    leaving the pointer's phase alone) fails here on the live view's phase while
    the manifest already reads ``complete``.
    """
    _write_run(crew_home, phase="blocked", manifest=BLOCKED_MANIFEST)

    before = _live_row()
    assert before["effective_phase"] == "blocked"
    assert before["classification"] == "blocked"

    record = runs.repair_manifest_status(
        RUN, "complete", "the worker delivered its work and said so in its report"
    )
    assert record["previous_status"] == "blocked"

    live = _live_row()
    reflex = _reflex_row()

    # Neither reader reads the status the repair replaced.
    assert live["manifest_status"] == "complete"
    assert live["effective_phase"] != "blocked"
    assert live["effective_phase"] == "complete"
    assert live["stored_phase"] == "complete"

    # The two readers classify the run the same way, and it is not blocked.
    assert live["classification"] == reflex["classification"]
    assert live["recovery_classification"] == reflex["recovery_classification"]
    assert live["classification"] != "blocked"
    assert live["recovery"] == reflex["recovery"] == "review"

    # The live view no longer offers what a blocked run gets: the command to
    # resolve the blocker the repair replaced. What it offers instead is the
    # review the reflex's own pass acts on, so a coordinator reads one act and
    # not a second dispatch beside it.
    next_action = str(live.get("next_action") or "")
    assert "resolve the blocker" not in next_action
    assert not (live["effective_phase"] == "blocked" and "--role review" in next_action)
    assert "--role review" in next_action

    # The record states the move it made, so a reader of the repair sees the
    # phase it replaced beside the verdict it wrote.
    assert record["previous_phase"] == "blocked"
    assert record["pointer_phase"] == "complete"


def test_a_repair_with_no_live_pointer_still_rewrites_the_manifest(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A departed run has no phase to move, and its repair is not refused for it."""
    from reckon.crew import run_paths

    monkeypatch.setattr(run_paths, "crew_home", lambda: crew_home / "crew")
    monkeypatch.setattr(runs, "crew_home", lambda: crew_home / "crew")
    home = crew_home / "crew"
    run_directory = home / "runs" / RUN
    run_directory.mkdir(parents=True, exist_ok=True)
    manifest = run_directory / "manifest.md"
    manifest.write_text(BLOCKED_MANIFEST, encoding="utf-8")

    record = runs.repair_manifest_status(RUN, "complete", "the delivery was read")

    assert record["previous_phase"] is None
    assert record["pointer_phase"] is None
    assert "status: complete" in manifest.read_text(encoding="utf-8")
    assert not runs.pointer_path(RUN).exists()
    assert os.environ["RECKON_HOME"] == str(crew_home)
