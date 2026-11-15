"""A declared wait is judged against its own limit and its own probe.

Three sessions reported the same misreading of a parked run from three angles,
and all three come from judging a wait by something it never declared.

''The default horizon.'' A multi-hour compute wait was read overdue at 900 s
and sent to "investigate" while its probe reported the job RUNNING. The 900 is
the log-staleness bound, borrowed as a horizon because the declaration named
none; it is not a limit the wait asked to be held to, and a live answer is
evidence there is nothing to investigate.

''The answering probe.'' A run whose probe was a script rather than a
``squeue`` or ``sacct`` vector was read producer-gone, because the scheduler
query put beside the probe named no job and this host's silence was read as
the job's absence. A probe that answered has already said what the thing it
watches is doing, and no second-hand reading beside it can outvote that.

''The cancelled job.'' A probe naming a job the scheduler cancelled before it
started reported ``CANCELLED`` with an elapsed time of zero. ``CANCELLED`` is a
way a job ends, so the declaration's terminal list matched it and the sweep was
offered a resume against work that never ran. That state names the allocation's
cancellation, not the end of the condition the wait is on.

Every case synthesises its run under the temporary home the suite isolates, and
the workstation's own pointer directory is asserted untouched before and after,
because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_classification
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "declared-wait-fixture"
SESSION = "fixture-session"
STALE_AFTER_SECONDS = 900
NOW_SECONDS = 1_788_467_520.0
HOUR = 3600.0
BOUND = recovery_classification.WAIT_PRODUCER_GONE_SECONDS
RUN_IDS = (
    "r-running-over-the-default-horizon",
    "r-running-over-its-declared-horizon",
    "r-file-wait-over-the-default-horizon",
    "r-script-probe-that-answers",
    "r-probe-naming-a-cancelled-job",
    "r-probe-naming-a-cancelled-job-that-ran",
)


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


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _history(*entries: tuple[float, str, bool]) -> list[dict[str, object]]:
    """A recorded probe history: (seconds before now, observed, answered)."""
    return [
        {"at": _stamp(NOW_SECONDS - ago), "observed": observed, "answered": answered}
        for ago, observed, answered in entries
    ]


def _waiting_run(
    run_id: str,
    *,
    probe: list[str] | None = None,
    files: list[str] | None = None,
    script_body: str | None = None,
    probe_extra: tuple[str, ...] = (),
    terminal: tuple[str, ...] = ("COMPLETED", "FAILED"),
    started_seconds_ago: float = 10.0,
    declared_horizon: str | None = None,
    history: list[dict[str, object]] | None = None,
) -> dict:
    """One parked run, its manifest, and the pointer a wait is read from.

    The manifest's own modification time is fresh in every case, because the
    pointer reader drops a stale one; the wait's age is declared by
    ``wait_started_at``, which is the clock a long wait carries. A probe given
    as ``script_body`` is written beside the run and handed to this
    interpreter, because a probe that answers has to answer for real: the
    surface re-reads the declaration, so a stubbed verdict would prove nothing
    about the row a coordinator sees.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    if script_body is not None:
        script = directory / "probe.py"
        script.write_text(script_body, encoding="utf-8")
        probe = [sys.executable, str(script), *probe_extra]
    manifest = directory / "manifest.md"
    lines = [
        f"node: {run_id}",
        "status: waiting",
        "wait_condition: the compute job writes its final rows",
        f"wait_started_at: {_stamp(NOW_SECONDS - started_seconds_ago)}",
    ]
    if files is not None:
        lines.append(f"wait_file: {json.dumps(files)}")
    else:
        lines.append(f"wait_probe: {json.dumps(probe or [])}")
        lines.append(f"wait_terminal: {json.dumps(list(terminal))}")
    if declared_horizon is not None:
        lines.append(f"wait_expected: {declared_horizon}")
    lines.append("resume_brief: collect the rows and finish the report")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = NOW_SECONDS - 10.0
    os.utime(manifest, (stamp, stamp))
    _write_json(directory / "attempt.json", {"attempt": 1, "at": _stamp(NOW_SECONDS)})
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
        "session": SESSION,
        "session_id": SESSION,
        "created_at": _stamp(NOW_SECONDS - 4 * HOUR),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(manifest),
        "phase": "waiting",
        "attempt": 1,
        "process_alive": False,
        "pid": _absent_pid(),
        "launcher_host": socket.gethostname(),
    }
    record["node"] = {
        "id": run_id,
        "plan": "plan-a",
        "section": "a-declared-wait-is-judged-against-itself",
        "time_budget": "30m",
        "write_paths": ["reckon/crew/recovery_liveness.py"],
    }
    if history is not None:
        record["wait_probe_history"] = history
    _write_json(pointer_path(run_id), record)
    return record


def _row(run: dict, condition_test=None) -> dict:
    return recovery.classify_pointer(
        run,
        now_seconds=NOW_SECONDS,
        stale_after_seconds=STALE_AFTER_SECONDS,
        condition_test=condition_test,
    )


# ── The horizon is the wait's own, or its probe's ──────────────────────────


def test_a_running_wait_is_not_overdue_before_its_own_limit() -> None:
    """A live probe three hours in is working, not overdue.

    The default horizon is the log-staleness bound, and holding a scheduler's
    job to it is what sent a multi-hour compute wait to "investigate". A
    declaration that names its own horizon is still judged by that horizon, and
    a ``wait_file`` condition still ages into the alarm: the exemption is a
    live answer, and a derived "absent" is not one.
    """
    run = _waiting_run(
        RUN_IDS[0],
        script_body="print('RUNNING')\n",
        started_seconds_ago=3 * HOUR,
    )

    row = _row(run)

    # The defect first: the row held a live scheduler job to a log-staleness
    # bound and read it overdue three hours in.
    assert row["wait_overdue"] is False
    assert row["recovery_classification"] == "waiting"
    assert row["classification"] == "waiting"
    wait = row["external_wait"]
    assert wait["overdue"] is False
    assert wait["probe_live_state"] == "running"
    assert wait["overdue_basis"] == "probe"
    assert wait["expected_horizon_seconds"] == STALE_AFTER_SECONDS
    snapshot = recovery._watch_snapshot(
        run, moment=NOW_SECONDS, stall_seconds=STALE_AFTER_SECONDS
    )
    assert snapshot["state"] == "waiting"

    # Control: a wait that names its own horizon is judged by it, so the same
    # live probe past that horizon is overdue.
    declared = _waiting_run(
        RUN_IDS[1],
        script_body="print('RUNNING')\n",
        started_seconds_ago=3 * HOUR,
        declared_horizon="2h",
    )

    aged = _row(declared)

    assert aged["external_wait"]["expected_horizon_seconds"] == 2 * HOUR
    assert aged["external_wait"].get("overdue_basis") is None
    assert aged["wait_overdue"] is True
    assert aged["recovery_classification"] == "wait-aged"

    # Control: a file condition declares no probe of its own, so its absent
    # paths age into the alarm as before.
    file_wait = _waiting_run(
        RUN_IDS[2],
        files=["never-written/rows.jsonl"],
        started_seconds_ago=3 * HOUR,
    )

    quiet = _row(file_wait)

    assert quiet["external_wait"]["probe_live_state"] is None
    assert quiet["wait_overdue"] is True
    assert quiet["recovery_classification"] == "wait-aged"


# ── An answering probe is the wait's evidence ──────────────────────────────


def test_a_script_probe_that_answers_is_never_read_abandoned() -> None:
    """The probe answered, so nothing beside it can say the work has stopped.

    The probe is a script rather than a scheduler vector, which is the shape
    whose scheduler silence was read as "no scheduler job" and then as a dead
    producer, while the script itself reported that the work was still going.
    """
    run = _waiting_run(
        RUN_IDS[3],
        script_body="print('PENDING')\n",
        history=_history(
            (BOUND + 300, "PENDING", True),
            (BOUND + 60, "PENDING", True),
            (60, "PENDING", True),
        ),
    )

    row = _row(run)

    # The defect first: an answering probe was outvoted by the scheduler query
    # put beside it, and the parked run was read abandoned.
    assert "the run's own process is gone" not in row["detail"]
    assert row["recovery_classification"] == "waiting"
    assert row["classification"] == "waiting"
    wait = row["external_wait"]
    assert wait.get("producer_gone") is None
    assert wait["valid"] is True
    assert wait["probe_live_state"] == "pending"


# ── A cancelled job that never started is suspect ──────────────────────────


def test_a_probe_naming_a_cancelled_job_that_never_started_is_suspect() -> None:
    """CANCELLED with no elapsed time names the allocation, not the wait's end.

    The same state with an elapsed time behind it is the job ending, and is
    still read as the terminal state the declaration names.
    """
    cancelled = _waiting_run(
        RUN_IDS[4],
        script_body="print('CANCELLED 00:00:00')\n",
        probe_extra=("-j", "1310001"),
        terminal=("COMPLETED", "FAILED", "CANCELLED"),
    )

    row = _row(cancelled)
    assert row["external_wait"]["probe"][2:] == ["-j", "1310001"]

    # The defect first: a job cancelled in the queue matched the declaration's
    # own CANCELLED terminal and the row offered the resume it named.
    assert "ready to resume" not in row["detail"]
    assert "--advice continue" not in row["next_action"]
    assert row["wait_condition_state"] != "met"
    assert "the probe is suspect" in row["detail"]
    suspect = row["external_wait"]["suspect_probe"]
    assert suspect is not None
    assert suspect["jobs"] == ["1310001"]
    assert suspect["state"] == "cancelled"

    # Control: a job that ran before it was cancelled is a terminal state, and
    # the row offers the resume its declaration named.
    ran = _waiting_run(
        RUN_IDS[5],
        script_body="print('CANCELLED 01:02:03')\n",
        probe_extra=("-j", "1310001"),
        terminal=("COMPLETED", "FAILED", "CANCELLED"),
    )

    ended = _row(ran)

    assert ended["external_wait"]["suspect_probe"] is None
    assert ended["wait_condition_state"] == "met"
    assert "ready to resume" in ended["detail"]
