"""A foreign run is freed once the allocation it ran inside leaves the queue.

A worker dies with the allocation it was launched inside. The scheduler's node
inventory excludes login hosts, whose processes outlive jobs, so the node's own
job answer is the fact that frees a run whose launching host is a different
machine — read for the launcher job where the record names one, and for every
job on the node where it does not.

The scheduler query is replaced by a recording stand-in here: the reading is
asserted on scripted answers, and what the reading asked the scheduler is
asserted from the stand-in's own record of it.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch as launch
from reckon.crew import process_liveness, runs
from reckon.crew import recovery_liveness as liveness

RUN_ID = "r-foreign-allocation"
FOREIGN_HOST = "98dci4-clu-2001.example.invalid"
NODE = "98dci4-clu-2001"
LAUNCHED_AT = "2026-11-15T03:15:00Z"
BEFORE_LAUNCH = "2026-11-15T02:15:00Z"
AFTER_LAUNCH = "2026-11-15T19:15:00Z"


class Scheduler:
    """A scheduler query stand-in: records every argv and answers the script.

    ``jobs`` is the node's job answer; None is a failed query, the shape a
    scheduler that could not be asked answers. ``node_known`` false is a node
    the inventory does not name, which the reading must not turn into an
    absence of jobs.
    """

    def __init__(self, jobs: str | None = "", *, node_known: bool = True) -> None:
        self.jobs = jobs
        self.node_known = node_known
        self.queries: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str | None:
        self.queries.append(list(argv))
        if argv[0] == "scontrol":
            if not self.node_known:
                return ""
            return f"NodeName={NODE} NodeHostName={NODE} State=ALLOCATED\n"
        return self.jobs

    @property
    def job_queries(self) -> list[list[str]]:
        return [query for query in self.queries if query[0] == "squeue"]


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A temporary crew home, with the real one asserted untouched afterwards."""
    real = runs.crew_home()
    real_pointer = real / "live" / f"{RUN_ID}.json"
    real_run_directory = real / "runs" / RUN_ID
    before = [real_pointer.exists(), real_run_directory.exists()]
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    yield directory
    assert [real_pointer.exists(), real_run_directory.exists()] == before


def _foreign_pointer(directory: Path, **overrides) -> dict:
    """A live pointer for a worker launched on a different machine."""
    record = {
        "run_id": RUN_ID,
        "project": "scheduler-fixture",
        "repo": str(directory),
        "worktree": str(directory),
        "launch": "cli",
        "backend": "probe",
        "role": "implement",
        "launcher_host": FOREIGN_HOST,
        "launched_at": LAUNCHED_AT,
        "pid": _absent_pid(),
        "phase": "working",
        "attempt": 1,
        "log_path": str(directory / "stream.jsonl"),
        "created_at": LAUNCHED_AT,
        "node": {"id": "stranded-worker", "time_budget": "25m", "write_paths": []},
    }
    record.update(overrides)
    runs._write_json(runs.pointer_path(RUN_ID), record)
    return record


def _observation(directory: Path) -> dict:
    return json.loads(
        (directory / "launcher-host-observation.json").read_text(encoding="utf-8")
    )


def test_a_launcher_job_that_left_the_queue_frees_the_run(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) A cancelled launcher job frees it though a peer's job still runs.

    The observation of 15 November listed the old fleet job as CANCELLED and a
    peer session's debug job as RUNNING on the same node. The peer's job began
    about sixteen hours after the worker was launched, so it cannot be the
    allocation that held the worker, and the run is gone.
    """
    record = _foreign_pointer(crew_home, launcher_job_id="1319675")
    scheduler = Scheduler(
        f"1319675|CANCELLED|2026-11-15T02:00:00\n1320449|RUNNING|{AFTER_LAUNCH}\n"
    )
    monkeypatch.setattr(process_liveness, "_run_scheduler_query", scheduler)

    assert liveness.launcher_host_is_gone(record) is True

    observation = _observation(crew_home)
    assert observation["process_gone"] is True
    assert observation["relied_on"]["basis"] == "launcher-job"
    assert observation["relied_on"]["launcher_job_id"] == "1319675"
    assert observation["relied_on"]["launcher_job_state"] == "CANCELLED"
    assert observation["jobs_query"][-1] == "%i|%T|%S"
    assert scheduler.job_queries, "the reading never asked the node for its jobs"


def test_a_launcher_job_that_failed_to_boot_frees_the_run(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) Any state outside the live set means the job left the queue.

    BOOT_FAIL is terminal under a name no enumerated terminal list would carry:
    the reading asks whether the state is in flight, not what it is called.
    """
    record = _foreign_pointer(crew_home, launcher_job_id="1319675")
    scheduler = Scheduler(f"1319675|BOOT_FAIL|\n1320449|RUNNING|{AFTER_LAUNCH}\n")
    monkeypatch.setattr(process_liveness, "_run_scheduler_query", scheduler)

    assert liveness.launcher_host_is_gone(record) is True
    assert _observation(crew_home)["relied_on"]["launcher_job_state"] == "BOOT_FAIL"


def test_a_launcher_job_still_running_keeps_the_run_unknown(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A live launcher job holds the worker, whatever its start time."""
    record = _foreign_pointer(crew_home, launcher_job_id="1319168")
    scheduler = Scheduler(f"1319168|RUNNING|{BEFORE_LAUNCH}\n")
    monkeypatch.setattr(process_liveness, "_run_scheduler_query", scheduler)

    assert liveness.launcher_host_is_gone(record) is False
    assert _observation(crew_home)["process_gone"] is False


def test_a_record_with_no_launcher_job_is_freed_by_a_later_start(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) Nothing on the node can hold a job launched before it began.

    A pointer written before the launcher job was recorded names none. One
    terminal job cannot hold a worker, and the only live job began after the
    launch, so neither is the allocation the worker ran inside.
    """
    record = _foreign_pointer(crew_home)
    assert "launcher_job_id" not in record
    scheduler = Scheduler(
        f"1320441|COMPLETED|2026-11-14T21:00:00\n1320449|RUNNING|{AFTER_LAUNCH}\n"
    )
    monkeypatch.setattr(process_liveness, "_run_scheduler_query", scheduler)

    assert liveness.launcher_host_is_gone(record) is True

    observed = _observation(crew_home)
    assert observed["relied_on"]["basis"] == "node-jobs"
    assert observed["relied_on"]["launched_at"] == LAUNCHED_AT
    assert [
        row["job_id"] for row in observed["relied_on"]["live_jobs_started_after_launch"]
    ] == ["1320449"]


def test_a_live_job_that_started_before_the_launch_keeps_the_run_unknown(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) A job already running at the launch could be the run's allocation."""
    record = _foreign_pointer(crew_home)
    scheduler = Scheduler(
        f"1320441|COMPLETED|2026-11-14T21:00:00\n1320449|RUNNING|{BEFORE_LAUNCH}\n"
    )
    monkeypatch.setattr(process_liveness, "_run_scheduler_query", scheduler)

    assert liveness.launcher_host_is_gone(record) is False
    observed = _observation(crew_home)
    assert observed["process_gone"] is False
    assert observed["relied_on"]["job_id"] == "1320449"


def test_a_failed_query_keeps_the_run_unknown(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(e) A question that could not be asked proves nothing."""
    record = _foreign_pointer(crew_home)
    monkeypatch.setattr(process_liveness, "_run_scheduler_query", Scheduler(None))

    assert liveness.launcher_host_is_gone(record) is False


def test_an_unknown_node_keeps_the_run_unknown(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inventory that does not name the node is not an empty queue."""
    record = _foreign_pointer(crew_home)
    scheduler = Scheduler("", node_known=False)
    monkeypatch.setattr(process_liveness, "_run_scheduler_query", scheduler)

    assert liveness.launcher_host_is_gone(record) is False
    assert scheduler.job_queries == []


def _prepared_pointer(directory: Path, **overrides) -> dict:
    """A pointer on this host, as a CLI launch of a fresh run writes it."""
    return _foreign_pointer(directory, launcher_host=socket.gethostname(), **overrides)


def test_a_cli_launch_records_the_allocation_it_runs_inside(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI launch inside an ordinary allocation records its job id."""
    _prepared_pointer(crew_home)
    monkeypatch.delenv("FLEET_JOB_ID", raising=False)
    monkeypatch.setenv("SLURM_JOB_ID", "1319168")

    launch._prepare_attempt_records(
        crew_home,
        run_id=RUN_ID,
        attempt=2,
        attempt_kind="dispatch",
        attempt_started_at="2026-11-16T21:00:00Z",
    )

    assert runs.read_pointer(RUN_ID)["launcher_job_id"] == "1319168"


def test_a_cli_launch_prefers_the_allocation_the_fleet_published(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet's published job outranks an environment that inherited one."""
    _prepared_pointer(crew_home)
    monkeypatch.setenv("FLEET_JOB_ID", "1319168")
    monkeypatch.setenv("SLURM_JOB_ID", "4242")

    launch._prepare_attempt_records(
        crew_home,
        run_id=RUN_ID,
        attempt=2,
        attempt_kind="dispatch",
        attempt_started_at="2026-11-16T21:00:00Z",
    )

    assert runs.read_pointer(RUN_ID)["launcher_job_id"] == "1319168"


def test_an_in_harness_launch_records_no_launcher_job(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-harness run does not run inside the allocation it is started from."""
    _prepared_pointer(crew_home, launch="in-harness")
    monkeypatch.setenv("SLURM_JOB_ID", "1319168")

    launch._prepare_attempt_records(
        crew_home,
        run_id=RUN_ID,
        attempt=2,
        attempt_kind="dispatch",
        attempt_started_at="2026-11-16T21:00:00Z",
    )

    assert "launcher_job_id" not in runs.read_pointer(RUN_ID)


def test_a_placed_run_keeps_answering_through_its_placement_job(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose placement wrote a job id is not given a second one."""
    _prepared_pointer(crew_home, job_id="1319168")
    monkeypatch.setenv("SLURM_JOB_ID", "4242")

    launch._prepare_attempt_records(
        crew_home,
        run_id=RUN_ID,
        attempt=2,
        attempt_kind="dispatch",
        attempt_started_at="2026-11-16T21:00:00Z",
    )

    assert "launcher_job_id" not in runs.read_pointer(RUN_ID)
    assert runs.read_pointer(RUN_ID)["job_id"] == "1319168"


def test_no_allocation_records_no_launcher_job(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch made outside any allocation writes no launcher job."""
    _prepared_pointer(crew_home)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("SLURM_JOBID", raising=False)
    monkeypatch.delenv("FLEET_JOB_ID", raising=False)

    launch._prepare_attempt_records(
        crew_home,
        run_id=RUN_ID,
        attempt=2,
        attempt_kind="dispatch",
        attempt_started_at="2026-11-16T21:00:00Z",
    )

    assert "launcher_job_id" not in runs.read_pointer(RUN_ID)
