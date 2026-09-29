"""One placement reservation, published as one record, resolved by every project.

The placement design holds a single reservation for the whole fleet and runs
every worker inside it as a step. That only works if the reservation's job id
is published somewhere every project reads, so these cases pin the record as
unkeyed: the project that published the record is not the project that must
resolve it, and the roster that admits the placed runs counts them whichever
project dispatched them.

The scheduler is faked throughout — the property under test is reckon's
plumbing, how many allocations are asked for and which job id a dispatch
resolves, not what the cluster does with them.

A dispatch under a project that has not published a record still resolves it
(case 1), the roster counts placed runs across projects rather than project
membership (case 2), a backend declaring no placement is untouched and
unbounded (case 3), and a legacy per-project record is read so an existing
reservation is not orphaned (case 4). Case 1 is the case a restored per-project
keying turns red, and the mutation is executed and logged as the negative
control.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon._backends import LaunchPlan
from reckon.crew import placement, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RESOLVED_EXECUTABLE = "/opt/backends/bin/codex"
RESERVATION_ID = "1274051"


class FakeScheduler:
    """A scheduler that records what it was asked for and answers with an id."""

    def __init__(self, job_id: str = RESERVATION_ID) -> None:
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
    """Point the shared crew state at a temporary home before anything is written."""
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


def _step_job_id(argv: list[str]) -> str | None:
    """The reservation id a placed worker's argv declares, or None."""
    ids = [
        item.removeprefix("--jobid=") for item in argv if item.startswith("--jobid=")
    ]
    assert len(ids) <= 1, f"expected at most one --jobid in {argv}"
    return ids[0] if ids else None


def _hold_under_project(project: str) -> FakeScheduler:
    """Publish one reservation from a dispatch under ``project``."""
    scheduler = FakeScheduler()
    placement.ensure_reservation(
        project=project,
        session=f"s-{project}",
        runner=scheduler,
        alive_probe=lambda record: False,
    )
    return scheduler


def test_placement_under_one_project_resolves_a_record_another_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One unkeyed record replaces per-project keying.

    A dispatch under ``alpha`` publishes the reservation; a dispatch under
    ``beta``, a project that never heard of it, must still resolve that record's
    job id and place its worker as a step inside the one allocation. Restoring
    per-project keying makes ``beta`` resolve nothing here and this case fails.
    """
    _isolate(monkeypatch, tmp_path)
    bin_dir = _step_scheduler_bin(tmp_path)
    environment = {"PATH": str(bin_dir)}
    monkeypatch.setattr(
        placement, "reservation_alive", lambda record, runner=None: bool(record)
    )

    scheduler = _hold_under_project("alpha")
    beta = dispatch_module.apply_backend_placement(
        _plan(environment), _placed_backend(), "beta"
    )

    assert len(scheduler.allocations()) == 1
    assert placement.read_reservation("beta")["job_id"] == RESERVATION_ID
    assert _step_job_id(beta.argv) == RESERVATION_ID


def test_the_roster_counts_placed_runs_across_every_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The roster's population is the placed runs, fleet-wide.

    Asserted over one backend's population holding placed and unplaced runs
    from two projects, so the rule is exercised rather than merely satisfied by
    a homogeneous population: the unplaced runs are not counted, and the placed
    runs of both projects are.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": RESERVATION_ID})

    placed = {"placement": {"scheduler": "srun", "options": ["--partition=all"]}}
    unplaced = [
        {"run_id": f"r-unplaced-{index}", "project": "alpha"} for index in range(30)
    ]
    placed_runs = [
        {"run_id": f"r-alpha-{index}", "project": "alpha", **placed}
        for index in range(12)
    ] + [
        {"run_id": f"r-beta-{index}", "project": "beta", **placed}
        for index in range(12)
    ]

    # The thirty unplaced runs hold no seat: twenty four placed runs sit one
    # below the cap and admit.
    dispatch_module._refuse_over_reservation_roster(
        _placed_backend(), unplaced + placed_runs
    )

    # The twenty fifth placed run, dispatched by either project, is refused.
    with pytest.raises(runs.CrewError) as refused:
        dispatch_module._refuse_over_reservation_roster(
            _placed_backend(),
            unplaced
            + placed_runs
            + [{"run_id": "r-beta-12", "project": "beta", **placed}],
        )
    assert "25" in str(refused.value)


def test_a_backend_declaring_no_placement_is_untouched_and_unbounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A backend with no placement runs no steps and holds no seat in the roster."""
    _isolate(monkeypatch, tmp_path)
    bin_dir = _step_scheduler_bin(tmp_path)
    placement.publish_reservation({"job_id": RESERVATION_ID})

    unplaced_backend = {"launch": "cli", "command": "codex"}
    plan = _plan({"PATH": str(bin_dir)})
    wrapped = dispatch_module.apply_backend_placement(plan, unplaced_backend)

    assert wrapped is plan
    # Forty placed runs of the reservation do not bound a backend that declares
    # no placement, because it places nothing into the allocation.
    occupying = [
        {
            "run_id": f"r-{index}",
            "placement": {"scheduler": "srun", "options": []},
        }
        for index in range(40)
    ]
    dispatch_module._refuse_over_reservation_roster(unplaced_backend, occupying)


def test_a_legacy_per_project_record_is_read_for_its_own_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An existing per-project record is not orphaned by the unkeyed one.

    Today's on-disk shape is ``placement/<project>/reservation.json``. With no
    unkeyed record present, a dispatch under that project still resolves the
    legacy record's job id, while a different project does not inherit it.
    """
    _isolate(monkeypatch, tmp_path)
    bin_dir = _step_scheduler_bin(tmp_path)
    environment = {"PATH": str(bin_dir)}
    monkeypatch.setattr(
        placement, "reservation_alive", lambda record, runner=None: bool(record)
    )

    legacy = placement.legacy_reservation_path("imas-ambix")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"job_id": RESERVATION_ID, "scheduler": "salloc"}),
        encoding="utf-8",
    )
    assert not placement.reservation_path().exists()

    plan = dispatch_module.apply_backend_placement(
        _plan(environment), _placed_backend(), "imas-ambix"
    )

    assert placement.read_reservation("imas-ambix")["job_id"] == RESERVATION_ID
    assert _step_job_id(plan.argv) == RESERVATION_ID
    # A project with neither record of its own reads nothing.
    assert placement.read_reservation("unrelated") is None
