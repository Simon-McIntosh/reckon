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

from reckon import host
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
    # The directory this pointer names is the run's home, and a real one already
    # holds the stream the launch opened there. A reading of the run writes its
    # launcher-host observation into that home, and it may not be the write that
    # brings the home into being, so the home is set up here rather than made
    # empty.
    (directory / "stream.jsonl").write_text(
        '{"type":"turn.started"}\n', encoding="utf-8"
    )
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


def test_a_launcher_job_purged_from_the_queue_frees_the_run(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) A job the answer no longer lists has left the queue.

    The scheduler keeps a finished job in ``squeue --states all`` only for its
    retention window, so a run read after that window finds its launcher job
    absent rather than reporting a terminal state. An answer that was taken and
    does not name the job is the queue saying the job is no longer in it, and
    the peer job still running on the node does not hold this run — which is
    the distinction from the failed query below, where no answer was taken and
    the run stays unknown.
    """
    record = _foreign_pointer(crew_home, launcher_job_id="1319675")
    scheduler = Scheduler(f"1320449|RUNNING|{AFTER_LAUNCH}\n")
    monkeypatch.setattr(process_liveness, "_run_scheduler_query", scheduler)

    assert liveness.launcher_host_is_gone(record) is True

    observed = _observation(crew_home)
    assert observed["process_gone"] is True
    assert observed["relied_on"]["launcher_job_id"] == "1319675"
    assert observed["relied_on"]["launcher_job_state"] is None
    assert "1320449" not in json.dumps(observed["relied_on"])


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
    # The start time is the only fact that differs between these two answers, so
    # the pair is the assertion: one live job frees the run from one side of the
    # launch and keeps it unknown from the other, and the terminal job is beside
    # the point either way.
    for started, expected in ((AFTER_LAUNCH, True), (BEFORE_LAUNCH, False)):
        scheduler = Scheduler(
            f"1320441|COMPLETED|2026-11-14T21:00:00\n1320449|RUNNING|{started}\n"
        )
        monkeypatch.setattr(process_liveness, "_run_scheduler_query", scheduler)

        assert liveness.launcher_host_is_gone(record) is expected, started

        observed = _observation(crew_home)
        assert observed["relied_on"]["basis"] == "node-jobs"
        dismissed = [
            row["job_id"]
            for row in observed["relied_on"].get("live_jobs_started_after_launch", ())
        ]
        assert dismissed == (["1320449"] if expected else []), started


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


def _placement(
    tmp_path: Path, *, environ: dict[str, str], cgroup: str
) -> host.HostFacts:
    """The host's own placement reading, over fixtures rather than this machine."""
    return host.host_facts(
        environ=environ,
        self_cgroup=cgroup,
        cgroup_root=tmp_path / "cgroup",
        mountinfo_path=tmp_path / "mountinfo",
        tmp_path=tmp_path / "tmp",
        home_path=tmp_path / "home",
    )


def _launch_records(
    crew_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    environ: dict[str, str],
    cgroup: str,
    **pointer: object,
) -> dict:
    """Prepare an attempt as a CLI launch under one placement reading."""
    _prepared_pointer(crew_home, **pointer)
    placement = _placement(tmp_path, environ=environ, cgroup=cgroup)
    monkeypatch.setattr(launch, "_current_host_facts", lambda: placement)
    launch._prepare_attempt_records(
        crew_home,
        run_id=RUN_ID,
        attempt=2,
        attempt_kind="dispatch",
        attempt_started_at="2026-11-16T21:00:00Z",
    )
    return runs.read_pointer(RUN_ID)


def _job_cgroup(job_id: str) -> str:
    return f"0::/system.slice/slurmstepd.scope/job_{job_id}/step_0"


NO_JOB_CGROUP = "0::/user.slice/user-1.slice/session-1.scope"


def test_a_cli_launch_records_the_allocation_it_runs_inside(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CLI launch inside an ordinary allocation records its cgroup's job."""
    pointer = _launch_records(
        crew_home,
        monkeypatch,
        tmp_path,
        environ={"SLURM_JOB_ID": "1319168"},
        cgroup=_job_cgroup("1319168"),
    )

    assert pointer["launcher_job_id"] == "1319168"


def test_a_cli_launch_prefers_the_allocation_the_fleet_published(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fleet's published job outranks a ``SLURM_JOB_ID`` it inherited.

    The fleet supervisor exports ``FLEET_JOB_ID`` for everything it starts, so
    a process outside any allocation but carrying one of its variables must not record
    the variable it inherited instead of the allocation the fleet holds.
    """
    pointer = _launch_records(
        crew_home,
        monkeypatch,
        tmp_path,
        environ={"FLEET_JOB_ID": "1319168", "SLURM_JOB_ID": "4242"},
        cgroup=NO_JOB_CGROUP,
    )

    assert pointer["launcher_job_id"] == "1319168"


def test_the_allocation_this_process_sits_in_outranks_the_fleet_variable(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An sbatch job submitted from inside the fleet records its own allocation.

    ``FLEET_JOB_ID`` is inherited by everything the fleet starts, a submitted
    job included, while that job's own cgroup names the allocation it actually
    runs inside. Recording the fleet's id there would name an allocation this
    launch never ran in.
    """
    pointer = _launch_records(
        crew_home,
        monkeypatch,
        tmp_path,
        environ={"FLEET_JOB_ID": "1319168", "SLURM_JOB_ID": "1320600"},
        cgroup=_job_cgroup("1320600"),
    )

    assert pointer["launcher_job_id"] == "1320600"


def test_the_slurm_jobid_alias_is_read_as_one_name(
    crew_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The old ``SLURM_JOBID`` spelling names the same allocation."""
    pointer = _launch_records(
        crew_home,
        monkeypatch,
        tmp_path,
        environ={"SLURM_JOBID": "1319168"},
        cgroup=_job_cgroup("1319168"),
    )

    assert pointer["launcher_job_id"] == "1319168"


def test_an_in_harness_launch_records_no_launcher_job(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An in-harness run does not run inside the allocation it is started from."""
    pointer = _launch_records(
        crew_home,
        monkeypatch,
        tmp_path,
        environ={"SLURM_JOB_ID": "1319168"},
        cgroup=_job_cgroup("1319168"),
        launch="in-harness",
    )

    assert "launcher_job_id" not in pointer


def test_a_placed_run_keeps_answering_through_its_placement_job(
    crew_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A run whose placement wrote a job id is not given a second one."""
    pointer = _launch_records(
        crew_home,
        monkeypatch,
        tmp_path,
        environ={"SLURM_JOB_ID": "4242"},
        cgroup=_job_cgroup("4242"),
        job_id="1319168",
    )

    assert "launcher_job_id" not in pointer
    assert pointer["job_id"] == "1319168"


def test_no_allocation_records_no_launcher_job(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch made outside any allocation writes no launcher job."""
    pointer = _launch_records(
        crew_home,
        monkeypatch,
        tmp_path,
        environ={},
        cgroup=NO_JOB_CGROUP,
    )

    assert "launcher_job_id" not in pointer


def test_a_pointer_naming_another_machine_records_no_launcher_job(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The field describes the allocation this attempt runs inside.

    A pointer naming another machine is an attempt that ran there, so this
    host's allocation would name a job the run never ran in — and a reading
    that names a job its node's answer does not carry frees nothing. The
    node's answer here reports the foreign launcher job as finished, which is
    what lets the launch proceed past its own liveness guard to the write.
    """
    placement = _placement(
        tmp_path,
        environ={"SLURM_JOB_ID": "1319168"},
        cgroup=_job_cgroup("1319168"),
    )
    monkeypatch.setattr(launch, "_current_host_facts", lambda: placement)
    monkeypatch.setattr(
        process_liveness,
        "_run_scheduler_query",
        Scheduler("1320441|CANCELLED|2026-11-14T21:00:00\n"),
    )
    _foreign_pointer(crew_home)

    launch._prepare_attempt_records(
        crew_home,
        run_id=RUN_ID,
        attempt=2,
        attempt_kind="dispatch",
        attempt_started_at="2026-11-16T21:00:00Z",
    )

    assert "launcher_job_id" not in runs.read_pointer(RUN_ID)
