"""A wait whose producer is gone is a decision, not a wait.

A waiting run is held until its probe reads a declared terminal value, and
nothing notice when the work the probe watches has died: on 29 October a
power_convertor study died with 12 of 66 rows written while the run went on
reading as working and waiting for fifteen hours, with three chained sections
behind it.

The reading this module pins is the classifier's: a run is producer-gone when
no process of it is alive and that is a reading this host can stand behind, the
probe's value has not moved for the stillness bound, and no scheduler job the
wait names is still in the queue. The producer-gone reading is a duty for its
session — the only thing left that can decide is a resume past the wait or a
discard of the run — and it never reads as working.

Every fixture synthesises its run under the temporary home the suite isolates,
and the workstation's own pointer directory is asserted untouched, because an
isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligations, recovery, recovery_classification, resumption
from reckon.crew.runs import _write_json, pointer_path, read_pointer, runs_dir

PROJECT = "producer-gone-fixture"
SESSION = "fixture-session"
HOST = socket.gethostname()
OTHER_HOST = "another-login-node"
STALE_AFTER_SECONDS = 900
NOW_SECONDS = 1_788_853_920.0
BOUND = recovery_classification.WAIT_PRODUCER_GONE_SECONDS
RUN_IDS = (
    "r-wait-producer-gone",
    "r-wait-file-probe-over-live-worker",
    "r-wait-value-still-moving",
    "r-wait-launched-elsewhere",
    "r-wait-scheduler-job-alive",
    "r-wait-worker-killed-no-exit-record",
    "r-wait-scheduler-gone-with-exit-record",
    "r-wait-probe-terminal",
    "r-wait-scheduler-unreadable",
    "r-wait-answered-with-no-history",
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


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _history(*entries: tuple[float, str, bool]) -> list[dict[str, object]]:
    """A recorded probe history: (seconds before now, observed, answered)."""
    history: list[dict[str, object]] = []
    for ago, observed, answered in entries:
        history.append(
            {
                "at": _stamp(NOW_SECONDS - ago),
                "observed": observed,
                "answered": answered,
            }
        )
    return history


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _waiting_run(
    run_id: str,
    *,
    probe: list[str],
    terminal: tuple[str, ...] = ("COMPLETED", "FAILED"),
    history: list[dict[str, object]] | None = None,
    pid: int | None = None,
    launcher_host: str | None = HOST,
    exit_record: bool = False,
) -> dict:
    """One parked run with the manifest and pointer a wait needs.

    ``pid`` and ``launcher_host`` decide what the process table can say: a pid
    on this host is asked here, a pid recorded by another host is carried but
    unproven, and no pid at all reads as unknown. ``exit_record`` writes the
    supervisor's receipt, which is the end the run itself recorded.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "\n".join(
            (
                f"node: {run_id}",
                "status: waiting",
                "wait_condition: the study writes its final rows",
                f"wait_probe: {json.dumps(probe)}",
                f"wait_terminal: {json.dumps(list(terminal))}",
                "resume_brief: collect the rows and finish the report",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    stamp = NOW_SECONDS - 10.0
    os.utime(manifest, (stamp, stamp))
    _write_json(directory / "attempt.json", {"attempt": 1, "at": _stamp(NOW_SECONDS)})
    if exit_record:
        _write_json(
            directory / "exit.json",
            {
                "run_id": run_id,
                "attempt": 1,
                "exit_code": 0,
                "exited_at": _stamp(NOW_SECONDS - 60),
                "stream_records_seen": 40,
            },
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
        "session": SESSION,
        "session_id": SESSION,
        "created_at": _stamp(NOW_SECONDS - 4 * 3600),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(manifest),
        "phase": "waiting",
        "attempt": 1,
        "process_alive": False,
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "a-wait-whose-producer-is-gone-is-an-obligation",
            "time_budget": "30m",
            "write_paths": ["reckon/crew/recovery_classification.py"],
        },
    }
    if history is not None:
        record["wait_probe_history"] = history
    if pid is not None:
        record["pid"] = pid
        record["pid_start_time"] = recovery._process_start_time(pid)
    if launcher_host is not None:
        record["launcher_host"] = launcher_host
    _write_json(pointer_path(run_id), record)
    return record


def _row(run: dict, condition_test=None) -> dict:
    return recovery.classify_pointer(
        run,
        now_seconds=NOW_SECONDS,
        stale_after_seconds=STALE_AFTER_SECONDS,
        condition_test=condition_test,
    )


def _duty_kinds() -> list[str]:
    view = obligations.obligations(PROJECT, SESSION)
    return [str(item.get("kind") or "") for item in view["obligations"]]


def _state_file(directory: Path, value: str) -> Path:
    path = directory / "state.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")
    return path


def test_a_wait_over_a_gone_producer_is_a_duty_and_not_working() -> None:
    """No process, a value that has not moved past the bound, no job alive.

    The file wait's probe answers the same state for as long as the wait is
    healthy, so the value alone would say nothing; the run's own process tree
    and the stillness of that value are what say nobody is left to change it.
    """
    directory = runs_dir() / RUN_IDS[0]
    state = _state_file(directory, "RUNNING")
    run = _waiting_run(
        RUN_IDS[0],
        probe=["cat", str(state)],
        history=_history(
            (BOUND + 300, "RUNNING", True),
            (BOUND + 60, "RUNNING", True),
            (60, "RUNNING", True),
        ),
        pid=_absent_pid(),
    )

    row = _row(run)

    assert row["classification"] == "producer-gone"
    assert row["recovery_classification"] == "producer-gone"
    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["external_wait"]["producer_gone"]["still_seconds"] >= BOUND
    assert "the run's own process is gone" in row["detail"]
    assert "reckon crew resume" in row["next_action"]
    assert "reckon crew discard" in row["next_action"]
    # Not working, and not waiting either: the row names a stopped process.
    assert run["phase"] == "waiting"
    assert row["classification"] not in {"running", "waiting"}

    assert "run-producer-gone" in _duty_kinds()
    view = obligations.obligations(PROJECT, SESSION)
    duty = next(
        item for item in view["obligations"] if item["kind"] == "run-producer-gone"
    )
    assert duty["run_id"] == RUN_IDS[0]
    assert duty["observed"] == "RUNNING"
    assert duty["still_seconds"] >= BOUND
    assert duty["terminal"] == ["COMPLETED", "FAILED"]
    assert "cat" in duty["probe"]
    assert "resume" in duty["next_command"]


def test_a_file_wait_over_a_live_worker_stays_waiting(live_pid: int) -> None:
    """A probe that answers a constant exit code while the worker lives.

    This is the reading the value alone cannot make: ``test -e`` exits non-zero
    on every sweep until the file appears, so its answer never changes while
    the work producing it is running. A live process means the producer is not
    gone, so the run goes on waiting.
    """
    directory = runs_dir() / RUN_IDS[1]
    run = _waiting_run(
        RUN_IDS[1],
        probe=["test", "-e", str(directory / "never-written" / "rows.jsonl")],
        # The value has stood still past the bound and the probe now answers
        # nothing at all, so only the live process keeps this run waiting.
        history=_history(
            (BOUND + 300, "absent", True),
            (60, "unavailable", False),
        ),
        pid=live_pid,
    )

    row = _row(run)

    assert row["process_alive"] is True
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"


def test_a_wait_whose_value_is_still_moving_stays_waiting() -> None:
    """No live process, on this host, but the probe's value has just moved."""
    directory = runs_dir() / RUN_IDS[2]
    state = _state_file(directory, "RUNNING-NEXT")
    run = _waiting_run(
        RUN_IDS[2],
        probe=["cat", str(state)],
        history=_history(
            (BOUND + 300, "RUNNING", True),
            (60, "RUNNING-NEXT", True),
        ),
        pid=_absent_pid(),
    )

    row = _row(run)

    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert (
        recovery_classification._wait_value_still_seconds(
            run,
            wait=row["external_wait"],
            observation={
                "observed": "RUNNING-NEXT",
                "answered": True,
            },
            now_seconds=NOW_SECONDS,
        )
        < BOUND
    )
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"


def test_a_wait_launched_on_another_host_is_not_producer_gone() -> None:
    """A pid means nothing on another machine, so its death is not proven."""
    directory = runs_dir() / RUN_IDS[3]
    state = _state_file(directory, "RUNNING")
    run = _waiting_run(
        RUN_IDS[3],
        probe=["cat", str(state)],
        history=_history(
            (BOUND + 300, "RUNNING", True),
            (60, "RUNNING", True),
        ),
        pid=_absent_pid(),
        launcher_host=OTHER_HOST,
    )

    row = _row(run)

    assert row["process_alive"] is False
    assert row["liveness_proven"] is False
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"


def test_a_scheduler_job_still_queued_keeps_its_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with no process is normal while the job it started is running.

    The job is what is producing the work, so a still ``squeue`` output over a
    run with no process is a healthy wait, not a dead producer.
    """
    monkeypatch.setattr(
        recovery_classification, "_scheduler_job_alive", lambda probe: True
    )
    run = _waiting_run(
        RUN_IDS[4],
        probe=["squeue", "-h", "-j", "1293562"],
        terminal=("COMPLETED", "FAILED"),
        # The probe answers the same state it has reported for longer than the
        # bound; only the queued job keeps this run waiting.
        history=_history(
            (BOUND + 300, "RUNNING", True),
            (60, "RUNNING", True),
        ),
        pid=_absent_pid(),
    )

    row = _row(
        run,
        condition_test=lambda _pointer, _wait: {
            "state": "unknown",
            "observed": "RUNNING",
            "answered": True,
            "detail": "the job is still in the queue",
        },
    )

    assert row["process_alive"] is False
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"


def test_a_worker_killed_outright_is_producer_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job left the queue, the probe stopped answering, nothing wrote an end.

    The existing probe-did-not-answer branch needs an exit record, and a worker
    killed outright leaves none. With no process and a probe that reports
    nothing, the value's clock runs from the last observation it did report.
    """
    monkeypatch.setattr(
        recovery_classification, "_scheduler_job_alive", lambda probe: False
    )
    run = _waiting_run(
        RUN_IDS[5],
        probe=["squeue", "-h", "-j", "1293562"],
        history=_history(
            (BOUND + 300, "RUNNING", True),
            (BOUND + 120, "RUNNING", True),
        ),
        pid=_absent_pid(),
    )

    row = _row(
        run,
        condition_test=lambda _pointer, _wait: {
            "state": "unknown",
            "observed": "unavailable",
            "answered": False,
            "detail": "the scheduler query exited non-zero",
        },
    )

    assert row["exit_record"] is None
    assert row["classification"] == "producer-gone"
    assert row["external_wait"]["producer_gone"]["still_seconds"] >= BOUND
    assert "run-producer-gone" in _duty_kinds()


def test_an_unreadable_scheduler_keeps_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduler that cannot answer has not said the job is gone.

    ``squeue`` fails for reasons that say nothing about the job — a slot the
    query cannot reach, a scheduler in transition, a job name the cluster no
    longer resolves. Reading that answer as "no job alive" would call a
    producer gone on the strength of a question that was never answered, so the
    run goes on waiting until something does answer.
    """
    monkeypatch.setattr(
        recovery_classification, "_scheduler_job_alive", lambda probe: None
    )
    run = _waiting_run(
        RUN_IDS[8],
        probe=["squeue", "-h", "-j", "1293562"],
        history=_history(
            (BOUND + 300, "RUNNING", True),
            (60, "RUNNING", True),
        ),
        pid=_absent_pid(),
    )

    row = _row(
        run,
        condition_test=lambda _pointer, _wait: {
            "state": "unknown",
            "observed": "RUNNING",
            "answered": True,
            "detail": "the job is still in the queue",
        },
    )

    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"


def test_a_probe_that_answered_with_no_history_is_not_proven_still() -> None:
    """An answering probe with no recorded history has no stillness to measure.

    The wait's own age says how long the run has been parked, never how long
    the probe's value has stood still, so using it here would report stillness
    the host never observed. Only a probe that never answered at all falls back
    to that age, because then there is no observation to contradict it.
    """
    directory = runs_dir() / RUN_IDS[9]
    state = _state_file(directory, "RUNNING")
    run = _waiting_run(
        RUN_IDS[9],
        probe=["cat", str(state)],
        history=None,
        pid=_absent_pid(),
    )

    row = _row(run)

    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["wait_observed"] == "RUNNING"
    assert (
        recovery_classification._wait_value_still_seconds(
            run,
            wait=row["external_wait"],
            observation={"observed": "RUNNING", "answered": True},
            now_seconds=NOW_SECONDS,
        )
        is None
    )
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"


def test_an_ended_worker_with_an_exit_record_keeps_its_existing_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an exit record the existing probe-did-not-answer branch owns it."""
    monkeypatch.setattr(
        recovery_classification, "_scheduler_job_alive", lambda probe: False
    )
    run = _waiting_run(
        RUN_IDS[6],
        probe=["squeue", "-h", "-j", "1293562"],
        history=_history(
            (BOUND + 300, "RUNNING", True),
            (BOUND + 120, "RUNNING", True),
        ),
        exit_record=True,
    )

    row = _row(
        run,
        condition_test=lambda _pointer, _wait: {
            "state": "unknown",
            "observed": "unavailable",
            "answered": False,
            "detail": "the scheduler query exited non-zero",
        },
    )

    assert row["exit_record"] is not None
    assert row["classification"] == "stalled"
    assert row["recovery_classification"] == "stalled"


def test_a_probe_reaching_its_terminal_releases_the_run_as_before() -> None:
    """The wait ends normally: the terminal value is reported and a resume is offered."""
    directory = runs_dir() / RUN_IDS[7]
    state = _state_file(directory, "COMPLETED")
    run = _waiting_run(
        RUN_IDS[7],
        probe=["cat", str(state)],
        history=_history(
            (BOUND + 300, "RUNNING", True),
            (60, "COMPLETED", True),
        ),
        pid=_absent_pid(),
    )

    row = _row(run)

    assert row["wait_condition_state"] == "met"
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "ready"
    assert row["next_action"] == (
        f"reckon crew resume --run {RUN_IDS[7]} --advice continue"
    )


def test_the_sweep_records_the_history_the_reading_is_taken_from() -> None:
    """One per-sweep actor writes the observations, and prunes them by age.

    The stillness the classifier measures is the age of this history, so no
    reader may have to write it: a sweep records every observation it makes,
    and keeps the entries by age so the history always reaches back past the
    bound however often the sweep runs.
    """
    directory = runs_dir() / RUN_IDS[0]
    state = _state_file(directory, "RUNNING")
    _waiting_run(
        RUN_IDS[0],
        probe=["cat", str(state)],
        history=_history(
            (BOUND + 2 * resumption.DEFAULT_SWEEP_SECONDS + 600, "RUNNING", True),
            (BOUND + 60, "RUNNING", True),
            (BOUND - 60, "RUNNING", True),
        ),
        pid=_absent_pid(),
    )

    resumption.sweep(
        PROJECT,
        now=datetime.fromtimestamp(NOW_SECONDS, tz=UTC),
        condition_test=lambda _pointer, _wait: {
            "terminal": False,
            "observed": "RUNNING",
            "detail": "still running",
        },
    )

    recorded = read_pointer(RUN_IDS[0])["wait_probe_history"]
    times = [entry["at"] for entry in recorded]
    assert times == sorted(times)
    assert recorded[-1] == {
        "at": _stamp(NOW_SECONDS),
        "observed": "RUNNING",
        "answered": True,
    }
    # The entry older than the bound plus one sweep interval is gone, because a
    # newer entry already reaches past the bound; every entry that does reach
    # back past the bound is kept, so the clock still has its reference.
    assert len(recorded) == 3
    assert _stamp(NOW_SECONDS - (BOUND - 60)) in times
    assert _stamp(NOW_SECONDS - (BOUND + 60)) in times
    assert (
        _stamp(NOW_SECONDS - (BOUND + 2 * resumption.DEFAULT_SWEEP_SECONDS + 600))
        not in times
    )
