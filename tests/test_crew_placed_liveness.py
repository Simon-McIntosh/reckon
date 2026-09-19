"""A placed run's liveness and terminal state follow its job, not a pid.

The fault: a worker charged to a scheduler job has its recorded pid on a compute
node, so every liveness read that consults the local process table answers a
question about a different machine and reports a live worker as dead. The job is
the subject: the classifier asks the scheduler whether the job is still in the
system, the reaper judges a job that never started by its scheduler reason and
by the payload log rather than by the scheduler client's exit status, and a job
the scheduler ended for time or memory is named distinctly from a worker whose
work failed, because the remedy differs.
"""

from __future__ import annotations

import importlib

from reckon.crew import recovery, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PLACEMENT = {"scheduler": "srun", "options": ["--partition=all"]}
EXEC = "/opt/backends/bin/codex"


def _runner(state: str | None):
    """A scheduler query answering one fixed state, or None when unread."""

    def run(argv: list[str]) -> str | None:  # noqa: ARG001 - the state is fixed
        return state

    return run


# --- the accessor consults the job when the record carries a placement -------


def test_a_placed_run_reports_live_while_its_job_is_running() -> None:
    record = {"pid": 4242, "placement": PLACEMENT, "job_id": "1274056"}
    # The pid probe would answer False — the pid belongs to a compute node — so
    # a True here can only come from the job, which is asked before the pid.
    assert (
        runs.record_process_alive(
            record, alive=lambda _pid: False, job_alive=lambda _rec: True
        )
        is True
    )


def test_a_placed_run_reports_terminal_when_the_job_leaves_the_queue() -> None:
    record = {"pid": 4242, "placement": PLACEMENT, "job_id": "1274056"}
    assert runs.placement_job_alive(record, _runner("COMPLETED")) is False


def test_the_job_is_asked_through_a_scheduler_query() -> None:
    argv = runs._scheduler_state_argv(PLACEMENT, "1274056")
    assert argv == ["squeue", "-h", "-j", "1274056", "-o", "%T"]


def test_a_scheduler_reckon_cannot_query_falls_through_to_the_pid() -> None:
    record = {"pid": 4242, "placement": {"scheduler": "nomad"}, "job_id": "x"}
    # Neither a scheduler state nor a placement reckon can translate: the pid
    # probe decides, which is what keeps an unqueryable placement from being
    # read as a stopped job.
    assert runs.record_process_alive(record, alive=lambda _pid: True) is True


def test_an_unplaced_record_reads_the_pid_as_before() -> None:
    assert runs.record_process_alive({"pid": 4242}, alive=lambda _pid: True) is True
    assert runs.record_process_alive({"pid": 4242}, alive=lambda _pid: False) is False


def test_an_unreadable_scheduler_is_not_a_verdict() -> None:
    record = {"pid": 4242, "placement": PLACEMENT, "job_id": "1274056"}
    assert runs.placement_job_alive(record, _runner(None)) is None


# --- a job the scheduler ended at a time or memory limit ---------------------


def test_a_job_killed_for_time_is_not_a_worker_whose_work_failed() -> None:
    assert runs.scheduler_kill_class("TIMEOUT", None) == "job-timeout"
    assert runs.scheduler_kill_class("RUNNING", "TimeLimit") == "job-timeout"


def test_a_job_killed_for_memory_is_named_distinctly() -> None:
    assert runs.scheduler_kill_class("OUT_OF_MEMORY", None) == "job-out-of-memory"
    assert runs.scheduler_kill_class("CANCELLED", "oom-kill") == "job-out-of-memory"


def test_an_ordinary_end_is_not_a_scheduler_kill() -> None:
    assert runs.scheduler_kill_class("COMPLETED", None) is None


# --- the reaper records a job that never started, once -----------------------


def test_a_job_that_never_started_records_its_scheduler_reason(
    monkeypatch,
) -> None:
    monkeypatch.setattr(dispatch_module, "_placement_job_state", lambda _p, _j: "FAILED")
    monkeypatch.setattr(
        dispatch_module, "scheduler_job_reason", lambda _p, _j: "launch failed"
    )
    record = dispatch_module._launch_failure_record(
        {"backend": "alpha", "argv": ["/usr/bin/srun"], "stderr_path": "/nonexistent"},
        exit_status=1,
        placement=PLACEMENT,
        job_id="1274056",
    )
    assert record["kind"] == "launch-failed"
    assert record["scheduler_reason"] == "launch failed"
    assert record["scheduler_state"] == "FAILED"


def test_a_job_killed_for_memory_records_the_distinct_kind(monkeypatch) -> None:
    monkeypatch.setattr(
        dispatch_module, "_placement_job_state", lambda _p, _j: "OUT_OF_MEMORY"
    )
    monkeypatch.setattr(dispatch_module, "scheduler_job_reason", lambda _p, _j: None)
    record = dispatch_module._launch_failure_record(
        {"backend": "alpha", "argv": [], "stderr_path": "/nonexistent"},
        exit_status=137,
        placement=PLACEMENT,
        job_id="1274000",
    )
    assert record["kind"] == "job-out-of-memory"


def test_a_job_still_in_the_system_is_not_recorded_as_a_launch_failure(
    monkeypatch, tmp_path
) -> None:
    """The reaper does not judge a job that has not ended."""
    stream = tmp_path / "stream.jsonl"
    stream.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        dispatch_module, "_placed_record_identity", lambda _r: (PLACEMENT, "1274000")
    )
    monkeypatch.setattr(
        dispatch_module, "_placement_job_alive", lambda _p, _j: True
    )
    recorded: list = []
    monkeypatch.setattr(
        dispatch_module, "_mutate_pointer", lambda _r, _m: recorded.append(_r)
    )
    dispatch_module._record_launch_failure(
        {
            "run_id": "r-x",
            "backend": "alpha",
            "argv": [],
            "stderr_path": "/nonexistent",
            "stream_path": str(stream),
        },
        exit_status=0,
    )
    assert recorded == []


def test_the_payload_log_decides_that_the_work_ran(monkeypatch, tmp_path) -> None:
    """A step reporting COMPLETED while its payload aborted is not success.

    The scheduler client's exit status is zero, so only the payload log can say
    whether a turn ran: a non-empty stream is a turn, whatever the status says.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text('{"type":"result"}\n', encoding="utf-8")
    monkeypatch.setattr(
        dispatch_module, "_placed_record_identity", lambda _r: (PLACEMENT, "1274000")
    )
    recorded: list = []
    monkeypatch.setattr(
        dispatch_module, "_mutate_pointer", lambda _r, _m: recorded.append(_r)
    )
    dispatch_module._record_launch_failure(
        {
            "run_id": "r-x",
            "backend": "alpha",
            "argv": [],
            "stderr_path": "/nonexistent",
            "stream_path": str(stream),
        },
        exit_status=0,
    )
    assert recorded == []


# --- recovery reads the record's explicit command, not argv[0] --------------


def test_recovery_takes_the_recorded_command_over_the_scheduler_argv() -> None:
    record = {"command": "claude", "argv": ["/usr/bin/srun", "--job"], "dialect": "claude"}
    assert recovery._harness_command(record, record["argv"]) == "claude"


def test_recovery_falls_back_to_argv_for_a_record_without_the_field() -> None:
    record = {"argv": ["/opt/backend/bin/codex", "exec"], "dialect": "codex"}
    assert recovery._harness_command(record, record["argv"]) == "/opt/backend/bin/codex"


def test_recovery_falls_back_to_the_dialect_when_argv_is_empty() -> None:
    assert recovery._harness_command({"dialect": "codex"}, None) == "codex"