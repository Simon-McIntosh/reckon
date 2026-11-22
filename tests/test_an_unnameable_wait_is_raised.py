"""A worker that ends on a wait nothing can run reaches its coordinator.

A worker can write a wait block and then end with a working status, its probe
written as prose naming scheduler jobs instead of as an argument vector. The
waiting reader acts only on a manifest whose status is the declared wait, and
the reader that runs a probe refuses that shape, so nothing sweeps, resumes or
promotes the run. Read as still working it reaches no duty, and the coordinator
finds the run only by opening its manifest.

These cases pin the three readings:

* an ended run with ``status: in-progress`` and a prose wait probe reads
  ``unreadable``, carrying the status text and the probe text, and appears as
  exactly one obligation of its session whose reason is that same account;
* an ended run with a runnable wait whose probe has passed is resumed by the
  sweep, as before — the new reading must not take a liftable wait away;
* an ended run with ``status: complete`` keeps its terminal word.

Every fixture synthesises its run under the temporary configuration home the
suite isolates, and the workstation's own live-pointer directory is asserted
untouched, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

import reckon.crew.obligations as obligations_module
from reckon.crew import recovery, resumption
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "unnameable-wait-fixture"
SESSION = "s-unnameable-wait"
PROSE_RUN_ID = "r-prose-wait"
UNKNOWN_WORD_RUN_ID = "r-unknown-word-wait"
LIFTABLE_RUN_ID = "r-liftable-wait"
COMPLETE_RUN_ID = "r-complete-wait"
RUN_IDS = (PROSE_RUN_ID, UNKNOWN_WORD_RUN_ID, LIFTABLE_RUN_ID, COMPLETE_RUN_ID)

NOW_SECONDS = 1_788_853_920.0
MANIFEST_BASELINE_NS = 1_788_853_800_000_000_000

# The reported probe: two scheduler jobs named in prose where the reader
# expects a shell-free argument vector. Kept verbatim so the case covers the
# reading it complains about.
PROSE_PROBE = "sbatch jobs 1335599 and 1335600 finish and their logs land"


@pytest.fixture(autouse=True)
def _the_real_pointer_directory_is_untouched() -> None:
    """No case may reach the workstation's own live-pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    mine = [real_live / f"{run_id}.json" for run_id in RUN_IDS]

    assert not any(path.exists() for path in mine)
    yield
    assert not any(path.exists() for path in mine)


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _pointer(run_id: str, body: str) -> dict:
    """A live pointer for ``SESSION`` over a manifest whose worker has ended.

    The pid is beyond the ceiling and the pointer names this host, so the
    process table's answer is a measurement rather than an unproven absence.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    worktree = directory / "worktree"
    worktree.mkdir(exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    os.utime(manifest, ns=(MANIFEST_BASELINE_NS, MANIFEST_BASELINE_NS))
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(runs_dir()),
        "worktree": str(worktree),
        "launch": "cli",
        "argv": ["fixture-agent", "exec"],
        "backend": "fixture-lane",
        "role": "implement",
        "created_at": "2026-09-21T00:00:00Z",
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stream.err"),
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": MANIFEST_BASELINE_NS - 1_000_000_000,
        "phase": "working",
        "process_alive": False,
        "launcher_host": socket.gethostname(),
        "session_id": "fixture-session",
        "pid": _absent_pid(),
        "attempt": 1,
        "node": {
            "id": run_id,
            "plan": "delivered-work-lands-without-a-coordinator",
            "section": "unnameable-wait",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def _prose_wait_body(status: str) -> str:
    return (
        f"node: {PROSE_RUN_ID}\n"
        f"status: {status}\n"
        "wait_condition: waiting on scheduler jobs 1335599 and 1335600\n"
        f"wait_probe: {PROSE_PROBE}\n"
        "wait_terminal: the jobs leave the queue\n"
        "resume_brief: read the job logs and finish\n"
    )


def _rows(run_id: str) -> list[dict]:
    payload = obligations_module.obligations(PROJECT, SESSION)
    return [
        row for row in payload["obligations"] if str(row.get("run_id") or "") == run_id
    ]


def test_an_ended_run_on_a_prose_wait_is_raised_quoting_status_and_probe() -> None:
    """The reported case: the run must reach its coordinator, naming both.

    The reading is ``unreadable`` — the declaration is what is wrong — and the
    run owes exactly one duty whose reason is the classifier's own account.
    Both the status word and the probe text are in that account, because a
    repair needs the two lines it is editing.
    """
    run = _pointer(PROSE_RUN_ID, _prose_wait_body("in-progress"))

    classified = recovery.classify_pointer(run, now_seconds=NOW_SECONDS)

    assert classified["classification"] == "unreadable", classified["detail"]
    assert classified["recovery"] == "repair", classified["recovery"]
    assert "in-progress" in classified["detail"], classified["detail"]
    assert PROSE_PROBE in classified["detail"], classified["detail"]

    rows = _rows(PROSE_RUN_ID)

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["kind"] == "unreadable", row
    assert row["reason"] == classified["detail"], row
    assert "in-progress" in row["reason"], row
    assert PROSE_PROBE in row["reason"], row


def test_a_liftable_wait_is_still_resumed_by_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half the new reading must not take away: a runnable wait lifts.

    The declared condition is a file whose path has landed, so the sweep's own
    probe reports the terminal state and offers the run to the resume loop. A
    case that stopped at the classifier could not tell a preserved lift from a
    reading that quietly counted the run as unreadable.
    """
    directory = runs_dir() / LIFTABLE_RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "job.log"
    log_path.write_text("COMPLETED\n", encoding="utf-8")
    run = _pointer(
        LIFTABLE_RUN_ID,
        (
            f"node: {LIFTABLE_RUN_ID}\n"
            "status: waiting\n"
            "wait_condition: the scheduler job's log has landed\n"
            f"wait_file: {json.dumps([str(log_path)])}\n"
            "resume_brief: read the job result and finish\n"
        ),
    )

    classified = recovery.classify_pointer(run, now_seconds=NOW_SECONDS)
    # The declaration is read, not refused: the run is a wait the sweep lifts.
    assert classified["classification"] != "unreadable", classified["detail"]
    assert classified["classification"] in {"waiting", "ready"}, classified["detail"]

    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [run])
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])
    report = resumption.sweep(
        PROJECT,
        dry_run=True,
        now=datetime.fromtimestamp(NOW_SECONDS, tz=UTC),
    )

    assert [row["run_id"] for row in report["resumed"]] == [LIFTABLE_RUN_ID], report


def test_an_ended_run_with_a_complete_status_keeps_its_word() -> None:
    """A terminal word is not re-read: the run delivered whatever else it says."""
    run = _pointer(COMPLETE_RUN_ID, _prose_wait_body("complete"))

    classified = recovery.classify_pointer(run, now_seconds=NOW_SECONDS)

    assert classified["manifest_status"] == "complete", classified["detail"]
    assert classified["classification"] != "unreadable", classified["detail"]
    assert "wait nothing can probe" not in classified["detail"], classified["detail"]


def test_a_status_outside_the_vocabulary_names_the_word_and_the_wait() -> None:
    """The other half: an unrecognised status word is refused with the wait.

    The reader refuses the body before any classifier can judge it, so the
    refusal is the one place both the rejected word and the declared wait are
    still in hand; a coordinator repairing only the word would leave the wait
    unchanged.
    """
    run = _pointer(
        UNKNOWN_WORD_RUN_ID,
        _prose_wait_body("delivered-awaiting-coordinator-adjudication"),
    )

    classified = recovery.classify_pointer(run, now_seconds=NOW_SECONDS)

    assert classified["classification"] == "unreadable", classified["detail"]
    assert "delivered-awaiting-coordinator-adjudication" in classified["detail"], (
        classified["detail"]
    )
    assert PROSE_PROBE in classified["detail"], classified["detail"]
