"""A declared wait whose probe cannot answer is a stop the coordinator must lift.

A worker may end its turn holding a declared wait. The recovery sweep is the
mechanism that resumes such a run, and it resumes only on a probe that reports
a declared terminal state. When the probe cannot answer at all — the scheduler
query exits non-zero, or the command cannot run — the sweep never will, and a
row that goes on reading ``waiting`` with the sweep's promise sends the
coordinator to a mechanism that is not going to act.

The reading this module pins is narrower than "the probe did not report
terminal": a probe that answers with a state outside the declaration's terminal
list is still a probe doing its job, and its run is still waiting on something
that may yet arrive. Only a probe that produced no answer at all, over a worker
that recorded its own end for the current attempt, is the stop nothing can
lift. That run is classified with a halted word and its next action names the
resume command, because the resume is the only thing left that can carry it.

Every fixture synthesises its run under the temporary home the suite isolates,
and the workstation's own pointer directory is asserted untouched, because an
isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery, ticker
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "wait-nothing-can-lift-fixture"
HOST = socket.gethostname()
STALE_AFTER_SECONDS = 900
NOW_SECONDS = 1_788_853_920.0
RUN_IDS = (
    "r-wait-unanswering-probe-ended-worker",
    "r-wait-unanswering-probe-ended-worker-live",
    "r-wait-probe-answers-non-terminal",
    "r-wait-probe-flag-reworded-detail-failed",
    "r-wait-probe-flag-reworded-detail-answered",
    "r-wait-superseded-exit-record",
)


@pytest.fixture(autouse=True)
def _the_real_pointer_directory_is_untouched() -> None:
    """No case may reach the workstation's own live-pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    mine = [real_live / f"{run_id}.json" for run_id in RUN_IDS]

    assert not any(path.exists() for path in mine)
    yield
    assert not any(path.exists() for path in mine)


@pytest.fixture()
def live_pid():
    """A process this test owns, so the run's pid answers the process table."""
    child = subprocess.Popen(["sleep", "300"])
    try:
        yield child.pid
    finally:
        child.terminate()
        child.wait()


def _absent_path(directory: Path) -> Path:
    """A path that does not exist: the probe's ``test -e`` exits non-zero."""
    return directory / "never-written" / "still-running.txt"


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _waiting_run(
    run_id: str,
    *,
    probe: list[str],
    terminal: list[str] = ("COMPLETED", "FAILED"),
    exit_record: bool = False,
    pid: int | None = None,
    attempt: int = 1,
    exit_attempt: int = 1,
    worker_attempt: int | None = None,
) -> dict:
    """One parked run with its manifest, attempt record and optional receipts.

    ``exit_record`` writes the supervisor's receipt stamped ``exit_attempt``.
    ``attempt`` is the attempt the run's own attempt record names, so the two
    can be made to disagree: a run on attempt 2 still holding attempt 1's
    receipt. ``worker_attempt`` writes a worker record naming that attempt —
    the record a resumed run holds for the attempt now running — with a pid no
    process will answer for. ``pid`` names a live process the run holds
    instead, so the same declaration can be read over a worker that has not
    stopped.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "\n".join(
            (
                f"node: {run_id}",
                "status: waiting",
                "wait_condition: scheduler job 1293562 has left the queue",
                f"wait_probe: {json.dumps(probe)}",
                f"wait_terminal: {json.dumps(list(terminal))}",
                "resume_brief: collect the scheduler result and finish the report",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    stamp = NOW_SECONDS - 10.0
    os.utime(manifest, (stamp, stamp))
    _write_json(
        directory / "attempt.json", {"attempt": attempt, "at": "2026-09-15T00:00:00Z"}
    )
    if exit_record:
        _write_json(
            directory / "exit.json",
            {
                "run_id": run_id,
                "attempt": exit_attempt,
                "exit_code": 0,
                "exited_at": "2026-09-15T00:05:00Z",
                "stream_records_seen": 40,
            },
        )
    if worker_attempt is not None:
        _write_json(
            directory / "worker.json",
            {"attempt": worker_attempt, "pid": _absent_pid()},
        )
    tree = runs_dir() / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(runs_dir()),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "codex",
        "role": "implement",
        "session_id": f"fixture-session-{run_id}",
        "created_at": "2026-09-15T00:00:00Z",
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(manifest),
        "phase": "waiting",
        "attempt": attempt,
        "process_alive": None if pid is not None else False,
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "a-wait-nothing-can-lift-needs-the-coordinator",
            "time_budget": "30m",
            "write_paths": ["reckon/crew/recovery_classification.py"],
        },
    }
    if pid is not None:
        record["pid"] = pid
        record["pid_start_time"] = recovery._process_start_time(pid)
        record["launcher_host"] = HOST
    _write_json(pointer_path(run_id), record)
    return record


def _row(run: dict, condition_test=None) -> dict:
    return recovery.classify_pointer(
        run,
        now_seconds=NOW_SECONDS,
        stale_after_seconds=STALE_AFTER_SECONDS,
        condition_test=condition_test,
    )


def _pane_word(row: dict) -> str:
    return ticker._pane_word(
        {
            "recovery_classification": row["recovery_classification"],
            "to_state": row["classification"],
        }
    )


def test_an_unanswering_probe_over_an_ended_worker_needs_the_coordinator() -> None:
    """The stop nothing lifts: the worker ended, the probe cannot answer.

    The sweep resumes on a terminal probe answer and will never see one here,
    so the row must not go on promising it. It carries a halted word and names
    the resume — the only action left — and says the probe could not answer.
    """
    directory = runs_dir() / RUN_IDS[0]
    run = _waiting_run(
        RUN_IDS[0],
        probe=["test", "-e", str(_absent_path(directory))],
        exit_record=True,
    )

    row = _row(run)

    assert row["external_wait"] is not None
    assert row["external_wait"]["valid"] is True
    assert row["wait_condition_state"] == "unknown"
    assert row["exit_record"] is not None
    assert _pane_word(row) == "halted", row["recovery_classification"]
    assert "reckon crew resume" in row["next_action"]
    assert "the recovery sweep will resume" not in row["next_action"]
    assert "did not answer" in row["detail"]


def test_a_live_worker_holding_the_wait_stays_waiting(live_pid: int) -> None:
    """The same declaration over a worker that has not stopped is unchanged.

    Nothing in the classification here observes an end, so the run is still
    waiting on its condition and the sweep is still the mechanism that will
    carry it once the probe can answer.
    """
    directory = runs_dir() / RUN_IDS[1]
    run = _waiting_run(
        RUN_IDS[1],
        probe=["test", "-e", str(_absent_path(directory))],
        pid=live_pid,
    )

    row = _row(run)

    assert row["process_alive"] is True
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"
    assert _pane_word(row) == "waiting"
    assert "the recovery sweep will resume" in row["next_action"]


def test_a_probe_answering_non_terminal_keeps_the_wait_waiting() -> None:
    """A probe that answers outside its terminal list is a probe at work.

    It reported ``RUNNING``: a real state, not the declared terminal one —
    the probe answered: the sweep resumes this run the moment the state it
    prints becomes terminal. Only a probe that answered nothing is the stop
    nothing can lift, so this run stays waiting even though its worker ended.
    """
    directory = runs_dir() / RUN_IDS[2]
    state_file = directory / "state.txt"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("RUNNING\n", encoding="utf-8")
    run = _waiting_run(
        RUN_IDS[2],
        probe=["cat", str(state_file)],
        exit_record=True,
    )

    row = _row(run)

    assert row["exit_record"] is not None
    assert row["wait_condition_state"] == "unknown"
    assert row["wait_observed"] == "RUNNING"
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"
    assert _pane_word(row) == "waiting"
    assert "the recovery sweep will resume" in row["next_action"]


def test_the_probe_flag_decides_not_the_reworded_detail_text() -> None:
    """Whether the probe answered is a structured fact, not the reader's prose.

    The failure detail is wording for a person and changes whenever the reader
    is reworded, so the halted reading tests the flag the probe result carries
    and not its text. Both observations below are worded nothing like the
    reader's own details; only the flag differs, and the reading follows it.
    """
    failed_run = _waiting_run(
        RUN_IDS[3],
        probe=["test", "-e", "/nonexistent/rewording-subject"],
        exit_record=True,
    )
    failed_row = _row(
        failed_run,
        condition_test=lambda _pointer, _wait: {
            "state": "unknown",
            "observed": "unavailable",
            "answered": False,
            "detail": "the scheduler page did not render this cycle",
        },
    )
    assert _pane_word(failed_row) == "halted", failed_row["recovery_classification"]
    assert "reckon crew resume" in failed_row["next_action"]

    answered_run = _waiting_run(
        RUN_IDS[4],
        probe=["test", "-e", "/nonexistent/rewording-subject"],
        exit_record=True,
    )
    answered_row = _row(
        answered_run,
        condition_test=lambda _pointer, _wait: {
            "state": "unknown",
            "observed": "RUNNING",
            "answered": True,
            "detail": "the scheduler page is still being polled",
        },
    )
    assert answered_row["classification"] == "waiting"
    assert _pane_word(answered_row) == "waiting"
    assert "the recovery sweep will resume" in answered_row["next_action"]


def test_a_superseded_attempts_exit_record_keeps_the_wait_waiting() -> None:
    """Only the current attempt's receipt may read as the worker's end.

    A resumed run reuses the run directory, so attempt 1's exit record sits
    beside the attempt record naming attempt 2. That record describes a
    predecessor whose end says nothing about the attempt now running, so the
    probe's failure cannot be read as a stop nothing can lift: the run stays
    waiting on its condition.
    """
    run = _waiting_run(
        RUN_IDS[5],
        probe=["test", "-e", "/nonexistent/current-attempt-still-running"],
        exit_record=True,
        exit_attempt=1,
        attempt=2,
        worker_attempt=2,
    )

    row = _row(run)

    assert row["external_wait"] is not None
    assert row["external_wait"]["valid"] is True
    # The predecessor's receipt is still carried on the row; what it is not is
    # this attempt's end.
    assert row["exit_record"] is not None
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"
    assert _pane_word(row) == "waiting"
    assert "the recovery sweep will resume" in row["next_action"]
