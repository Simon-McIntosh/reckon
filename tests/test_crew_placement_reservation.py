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


def test_an_unheld_reservation_leaves_the_declared_wrapping_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    scheduler_bin = _step_scheduler_bin(tmp_path)
    plan = _plan({"PATH": str(scheduler_bin)})

    wrapped = dispatch_module.apply_backend_placement(plan, _placed_backend())

    assert "--overlap" not in wrapped.argv
    assert [Path(wrapped.argv[0]).name, *wrapped.argv[1:3]] == [
        "srun",
        "--partition=all",
        RESOLVED_EXECUTABLE,
    ]


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
    occupying = [{"run_id": f"r-{index}"} for index in range(25)]

    with pytest.raises(runs.CrewError) as refused:
        dispatch_module._refuse_over_reservation_roster(_placed_backend(), occupying)

    assert "25" in str(refused.value)
    assert "resident memory per worker" in str(refused.value)


def test_a_reservation_counts_only_the_workers_of_the_project_that_armed_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The roster bounds one project's workers, never every project's.

    Measured 2026-09-23: one project declared a placement, the record it
    published was host-global, and forty five runs across four repositories
    were counted against a ceiling of twenty five while exactly one step ran
    inside the allocation. Dispatch was refused workstation wide for four
    minutes. The cap was right and its population was not.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"}, "alpha")

    # Twenty four of alpha's own, and a crowd belonging to other projects.
    occupying = [
        {"run_id": f"r-alpha-{index}", "project": "alpha"} for index in range(24)
    ] + [{"run_id": f"r-beta-{index}", "project": "beta"} for index in range(40)]

    # Alpha is one below its cap, so the foreign forty do not refuse it.
    dispatch_module._refuse_over_reservation_roster(
        _placed_backend(), occupying, "alpha"
    )

    # Its own twenty fifth does.
    with pytest.raises(runs.CrewError) as refused:
        dispatch_module._refuse_over_reservation_roster(
            _placed_backend(),
            occupying + [{"run_id": "r-alpha-24", "project": "alpha"}],
            "alpha",
        )
    assert "25" in str(refused.value)

    # And beta, holding no reservation of its own, is unbounded by alpha's.
    dispatch_module._refuse_over_reservation_roster(
        _placed_backend(), occupying, "beta"
    )


def test_a_host_global_record_belongs_to_no_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pre-project record is read, and bounds nobody by inheritance.

    An existing host-global record must not silently become every project's
    reservation on upgrade, which is the fleet-wide ceiling this keying exists
    to remove. It stays readable so it is not orphaned, and a named project
    does not fall back to it.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})

    assert placement.read_reservation() is not None
    assert placement.read_reservation("alpha") is None

    occupying = [
        {"run_id": f"r-alpha-{index}", "project": "alpha"} for index in range(40)
    ]
    dispatch_module._refuse_over_reservation_roster(
        _placed_backend(), occupying, "alpha"
    )


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
