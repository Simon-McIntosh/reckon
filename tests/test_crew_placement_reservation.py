"""One reservation held once, workers placed into it as steps, and a cap we own.

A job per worker mints an allocation per worker, so concurrency becomes a
matter of submitting more reservations rather than of sizing one, and nothing
is shared between the sessions on a host. These cases pin the opposite: a
single reservation held by an ensure command and published where every session
reads it, a second call that starts nothing, workers from two different
sessions running as steps inside the one allocation, and a roster cap that
refuses the worker past it naming the memory axis it was sized on.

The scheduler is faked throughout. The property under test is reckon's
plumbing — how many allocations are asked for, how many steps are placed and
under which job id — not what the cluster does with them, and a case that
needs a cluster would not be a case.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from reckon._backends import LaunchPlan
from reckon.crew import placement, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RESOLVED_EXECUTABLE = "/opt/backends/bin/codex"


class FakeScheduler:
    """A scheduler that records what it was asked for and answers with an id."""

    def __init__(self, job_id: str = "1274051") -> None:
        self.job_id = job_id
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if "salloc" in Path(argv[0]).name:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=f"salloc: Granted job allocation {self.job_id}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def allocations(self) -> list[list[str]]:
        return [call for call in self.calls if "salloc" in Path(call[0]).name]


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the shared crew state at a temp home before anything is written."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))


def _step_scheduler_bin(tmp_path: Path) -> Path:
    """A directory holding the step client, on no ambient PATH."""
    directory = tmp_path / "scheduler-bin"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("srun", "salloc"):
        executable = directory / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    return directory


def _plan(environment: dict) -> LaunchPlan:
    return LaunchPlan(
        backend="alpha",
        dialect="codex",
        argv=[RESOLVED_EXECUTABLE, "exec", "--task", "t"],
        cwd="/work/tree",
        stdin_text="",
        environment=environment,
        final_message_path=None,
        resumed_session=None,
    )


def _placed_backend() -> dict:
    return {
        "launch": "cli",
        "command": "codex",
        "placement": {"scheduler": "srun", "options": ["--partition=all"]},
    }


def test_the_ensure_command_holds_one_reservation_at_the_decided_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    scheduler = FakeScheduler()

    result = placement.ensure_reservation(
        session="s-1", runner=scheduler, alive_probe=lambda record: False
    )

    (argv,) = scheduler.allocations()
    assert argv[0] == "salloc"
    assert "--no-shell" in argv
    assert "--cpus-per-task=32" in argv
    assert "--mem=128G" in argv
    assert result["job_id"] == "1274051"
    assert result["started"] is True
    # Published where every session reads it, not held in the ensuring session.
    assert placement.read_reservation()["job_id"] == "1274051"
    assert runs.placement_ensure_line() == "reckon crew placement --ensure"


def test_a_second_ensure_reports_the_reservation_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    scheduler = FakeScheduler()
    placement.ensure_reservation(
        session="s-1", runner=scheduler, alive_probe=lambda record: False
    )

    second = placement.ensure_reservation(
        session="s-2", runner=scheduler, alive_probe=lambda record: True
    )

    assert len(scheduler.allocations()) == 1
    assert second["job_id"] == "1274051"
    assert second["started"] is False
    assert second["reason"] == "already-held"
    assert "started nothing" in second["detail"]


def test_a_reservation_whose_job_has_left_is_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})

    scheduler = FakeScheduler(job_id="1274099")
    result = placement.ensure_reservation(
        session="s-2", runner=scheduler, alive_probe=lambda record: False
    )

    assert result["started"] is True
    assert result["job_id"] == "1274099"
    assert result["record"]["replaced"] == "1274051"
    assert placement.read_reservation()["job_id"] == "1274099"


def test_two_sessions_place_two_workers_as_steps_in_one_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The property the shared design exists for, and one session cannot show it.

    Two dispatch calls under two session ids reach the same published job id:
    the second session finds the reservation already held, starts none of its
    own, and its worker still becomes a step inside the first session's
    allocation. A single-session case would pass just as well against a design
    that held one reservation per dispatcher.
    """
    _isolate(monkeypatch, tmp_path)
    scheduler = FakeScheduler()
    scheduler_bin = _step_scheduler_bin(tmp_path)
    environment = {"PATH": str(scheduler_bin)}
    # The reservation is live for both dispatches: liveness of a real job is a
    # cluster question and is asserted elsewhere, while this case is about how
    # many steps land under which published id.
    monkeypatch.setattr(
        placement, "reservation_alive", lambda record, runner=None: bool(record)
    )

    first = runs.ensure_placement_reservation(
        session="s-1",
        runner=scheduler,
        alive_probe=lambda record: False,
    )
    # The reservation is live for the second session, which is what a held
    # allocation looks like from anywhere but the session that took it.
    first_plan = dispatch_module.apply_backend_placement(
        _plan(environment), _placed_backend()
    )
    second = runs.ensure_placement_reservation(
        session="s-2",
        runner=scheduler,
        alive_probe=lambda record: record is not None,
    )
    second_plan = dispatch_module.apply_backend_placement(
        _plan(environment), _placed_backend()
    )

    assert len(scheduler.allocations()) == 1
    assert second["started"] is False
    assert first["job_id"] == second["job_id"] == "1274051"
    for plan in (first_plan, second_plan):
        step = Path(plan.argv[0]).name
        assert step == "srun"
        assert "--overlap" in plan.argv
        assert f"--jobid={first['job_id']}" in plan.argv
        assert plan.argv[-len(_plan(environment).argv) :] == _plan(environment).argv


def test_an_unheld_reservation_is_held_then_the_worker_is_placed_into_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolution holds the shared reservation before placing the worker in it.

    With no reservation readable, a dispatch must reach the ensure rather than
    with a bare step client: the holding command is called once for the project,
    the job id it holds is published, and the worker is placed into it as an
    overlapping step carrying that id.
    """
    _isolate(monkeypatch, tmp_path)
    scheduler_bin = _step_scheduler_bin(tmp_path)
    held_for: list[str | None] = []

    def record_hold(*, project: str | None = None, **_kwargs) -> dict:
        held_for.append(project)
        record = {"job_id": "1274051", "scheduler": "salloc", "options": []}
        placement.publish_reservation(record, project)
        return {"job_id": "1274051", "started": True, "record": record}

    monkeypatch.setattr(placement, "ensure_reservation", record_hold)
    monkeypatch.setattr(
        placement, "reservation_alive", lambda record, runner=None: bool(record)
    )
    plan = _plan({"PATH": str(scheduler_bin)})

    wrapped = dispatch_module.resolve_backend_placement(
        plan, _placed_backend(), "alpha"
    )

    assert held_for == ["alpha"]
    assert Path(wrapped.argv[0]).name == "srun"
    assert "--overlap" in wrapped.argv
    assert "--jobid=1274051" in wrapped.argv
    assert wrapped.argv[-len(plan.argv) :] == plan.argv


class FakeJobScheduler:
    """A scheduler answering one named job's state, owner and shape.

    The replacement's whole decision rests on the state and owner row — is the
    job running, is it this user's — so a fake that answers it is the instrument
    the case reads. The shape row is answered too, because the replacement
    resolves the target's shape through the fleet node's own reader. An
    ``exit_status`` carrying the unknown-job marker models this cluster, whose
    client reports a job id it does not know as an error rather than as an empty
    successful answer.
    """

    # The marker this cluster's client prints for a job id it does not know.
    UNKNOWN_JOB_MARKER = "slurm_load_jobs error: Invalid job id specified\n"

    def __init__(
        self,
        *,
        state: str | None = "RUNNING",
        user: str = "tester",
        partition: str = "all",
        cores: str = "16",
        memory: str = "64G",
        exit_status: int = 0,
        stderr: str = "",
    ) -> None:
        self.calls: list[list[str]] = []
        self.state = state
        self.user = user
        self.partition = partition
        self.cores = cores
        self.memory = memory
        self.exit_status = exit_status
        self.stderr = stderr

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        stdout = ""
        if self.exit_status == 0:
            fmt = argv[argv.index("-o") + 1] if "-o" in argv else ""
            if fmt == "%T|%u" and self.state is not None:
                stdout = f"{self.state}|{self.user}\n"
            elif fmt == "%P|%C|%m":
                stdout = f"{self.partition}|{self.cores}|{self.memory}\n"
        return subprocess.CompletedProcess(
            argv, self.exit_status, stdout=stdout, stderr=self.stderr
        )


def test_replacement_points_a_live_record_at_a_running_job_and_a_dispatch_names_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The property the flag exists for: the record moves, later placement follows.

    A live record naming one job is replaced with a running job of this user,
    the replaced id is recorded beside the new one, and a dispatch composed after
    the replacement places its worker under the new job id — never the old one.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})
    monkeypatch.setenv("USER", "tester")
    scheduler_bin = _step_scheduler_bin(tmp_path)
    scheduler = FakeJobScheduler(state="RUNNING", user="tester")
    # Liveness of a real job is a cluster question asserted elsewhere; this case
    # is about which id a later dispatch resolves later, so the record is held
    # live for the composition below.
    monkeypatch.setattr(
        placement, "reservation_alive", lambda record, runner=None: bool(record)
    )

    result = placement.replace_reservation(
        job_id="1274099", session="s-1", runner=scheduler
    )

    assert result["job_id"] == "1274099"
    assert result["replaced"] == "1274051"

    assert placement.read_reservation()["job_id"] == "1274099"
    assert placement.read_reservation()["replaced"] == "1274051"
    assert result["reason"] == "replaced"
    # The target's shape is read through the fleet node's own reader, so the
    # record describes the allocation the job actually carries.
    assert result["record"]["partition"] == "all"
    assert result["record"]["size"] == {"cores": 16, "memory_gb": 64}
    # Both ids are printed for the reader who asked for the move.
    assert "1274051" in result["detail"]
    assert "1274099" in result["detail"]

    plan = dispatch_module.apply_backend_placement(
        _plan({"PATH": str(scheduler_bin)}), _placed_backend()
    )
    assert Path(plan.argv[0]).name == "srun"
    assert "--overlap" in plan.argv
    assert "--jobid=1274099" in plan.argv
    assert "--jobid=1274051" not in plan.argv


def test_replacement_refuses_a_pending_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A job that has not started is not a place to run steps, and is refused."""
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})
    monkeypatch.setenv("USER", "tester")
    scheduler = FakeJobScheduler(state="PENDING", user="tester")

    with pytest.raises(runs.CrewError) as refused:
        placement.replace_reservation(job_id="1274099", runner=scheduler)

    message = str(refused.value)
    assert "is pending, not running" in message
    assert "point the reservation at a job that has started" in message
    # The record is untouched: a refusal that moved it would be worse than none.
    assert placement.read_reservation()["job_id"] == "1274051"


def test_replacement_refuses_another_users_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Another user's job is not ours to place workers into, and is refused."""
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})
    monkeypatch.setenv("USER", "tester")
    scheduler = FakeJobScheduler(state="RUNNING", user="someone-else")

    with pytest.raises(runs.CrewError) as refused:
        placement.replace_reservation(job_id="1274099", runner=scheduler)

    message = str(refused.value)
    assert "belongs to someone-else" in message
    assert "not this user (tester)" in message
    assert placement.read_reservation()["job_id"] == "1274051"


def test_replacement_refuses_a_job_the_scheduler_does_not_know(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A job id the scheduler does not carry is refused as an unknown job.

    This cluster's client answers an unknown job id with a non-zero exit and an
    ``Invalid job id specified`` error rather than with an empty successful
    answer, so the marker is what makes the refusal the does-not-know one and
    not a scheduler that could not be asked.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})
    monkeypatch.setenv("USER", "tester")
    scheduler = FakeJobScheduler(
        exit_status=1, stderr=FakeJobScheduler.UNKNOWN_JOB_MARKER
    )

    with pytest.raises(runs.CrewError) as refused:
        placement.replace_reservation(job_id="99999999", runner=scheduler)

    message = str(refused.value)
    assert "does not know job 99999999" in message
    assert "could not be asked" not in message
    assert placement.read_reservation()["job_id"] == "1274051"


def test_replacement_refuses_a_scheduler_that_cannot_be_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A question that never reached the scheduler must not read as an absent job.

    A client that exits non-zero without the unknown-job marker has said nothing
    about the job, so the refusal reports the failed query rather than claiming
    the scheduler does not know it, and the record is left unchanged either way.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})
    monkeypatch.setenv("USER", "tester")
    scheduler = FakeJobScheduler(
        exit_status=1, stderr="slurm_load_jobs error: Socket timed out on send/recv\n"
    )

    with pytest.raises(runs.CrewError) as refused:
        placement.replace_reservation(job_id="1274099", runner=scheduler)

    message = str(refused.value)
    assert "could not be asked about job 1274099" in message
    assert "Socket timed out" in message
    assert "does not know" not in message
    assert placement.read_reservation()["job_id"] == "1274051"


def test_the_roster_refuses_the_worker_past_the_cap_on_the_memory_axis() -> None:
    assert placement.reservation_roster_refusal(24) is None

    refusal = placement.reservation_roster_refusal(25)

    assert refusal is not None
    assert "25" in refusal
    assert str(placement.RESERVATION_ROSTER_LIMIT) == "25"
    # Sized on memory per worker against reserved memory, never a core count:
    # naming cores would invite raising the cap by counting the axis that does
    # not bind.
    assert "resident memory per worker" in refusal
    assert "not a core count" in refusal


def test_the_dispatch_admission_refuses_past_the_roster_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap is enforced where a dispatch is admitted, not only in a helper."""
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})
    placed = {"placement": {"scheduler": "srun", "options": ["--partition=all"]}}
    occupying = [{"run_id": f"r-{index}", **placed} for index in range(25)]

    with pytest.raises(runs.CrewError) as refused:
        dispatch_module._refuse_over_reservation_roster(_placed_backend(), occupying)

    assert "25" in str(refused.value)
    assert "resident memory per worker" in str(refused.value)


def test_the_roster_counts_placed_runs_across_every_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The roster counts the runs actually placed, fleet-wide, not by project.

    Before: the count was scoped to the reading project, so a run placed into
    the one allocation by another project occupied no seat in the roster that
    admits every project's workers, and a project holding no reservation of its
    own was unbounded by the reservation its runs actually ran inside.

    After: every run whose record names a placement occupies the one shared
    roster, and a run that was never placed holds no seat.

    Measured 2026-09-23, the defect this replaces: one project declared a
    placement, the record it published was host-global, and forty five runs
    across four repositories were counted against a ceiling of twenty five
    while exactly one step ran inside the allocation. The cap was right; its
    population was not.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})

    placed = {"placement": {"scheduler": "srun", "options": ["--partition=all"]}}
    occupying = (
        [
            {"run_id": f"r-alpha-{index}", "project": "alpha", **placed}
            for index in range(12)
        ]
        + [
            {"run_id": f"r-beta-{index}", "project": "beta", **placed}
            for index in range(12)
        ]
        + [{"run_id": f"r-gamma-{index}", "project": "gamma"} for index in range(12)]
    )

    # Twenty four placed runs of two projects sit one below the cap of twenty
    # five; the twelve runs that were never placed hold no seat.
    dispatch_module._refuse_over_reservation_roster(_placed_backend(), occupying)

    # The twenty fifth placed run, from either project, is refused.
    with pytest.raises(runs.CrewError) as refused:
        dispatch_module._refuse_over_reservation_roster(
            _placed_backend(),
            [*occupying, {"run_id": "r-beta-12", "project": "beta", **placed}],
        )
    assert "25" in str(refused.value)


def test_the_one_record_is_read_by_every_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The record one project publishes is the record every project reads.

    Before: a named project read only its own record and deliberately did not
    fall back to the unkeyed one, so a project that did not hold the allocation
    resolved nothing and every worker of a host-layer placement fell back to
    the declared wrapping. After: the record is unkeyed, so a project that
    never published it still resolves the shared allocation's job id.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})

    assert placement.read_reservation() is not None
    assert placement.read_reservation("alpha")["job_id"] == "1274051"
    assert placement.read_reservation("beta")["job_id"] == "1274051"


def test_a_placed_backend_is_bounded_by_the_shared_roster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A placed backend is bounded; the cap is not scoped to a project.

    A project whose runs are placed is bounded by the shared reservation's
    roster whichever project published the reservation.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})

    placed = {"placement": {"scheduler": "srun", "options": ["--partition=all"]}}
    occupying = [
        {"run_id": f"r-beta-{index}", "project": "beta", **placed}
        for index in range(25)
    ]
    with pytest.raises(runs.CrewError):
        dispatch_module._refuse_over_reservation_roster(_placed_backend(), occupying)


def test_an_unplaced_backend_has_no_roster_of_ours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})

    # A backend that declares no placement runs no steps and so has no seat in
    # our roster; the cap must not refuse a lane it does not govern.
    dispatch_module._refuse_over_reservation_roster(
        {"launch": "cli", "command": "codex"},
        [{"run_id": f"r-{index}"} for index in range(40)],
    )
