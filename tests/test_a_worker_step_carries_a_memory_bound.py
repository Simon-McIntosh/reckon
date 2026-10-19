"""Every placed worker step carries an explicit memory bound.

A step with no bound is a step that may grow to the allocation's own limit,
where the scheduler's OOM killer picks the largest process anywhere in the job
rather than the one that grew. The bound is stated by
:func:`reckon.crew.placement.step_prefix`, the one owner of a step's options,
and defaults to the worker share. A step sized past the allocation's ceiling
cannot sit beside the sessions at all: it is placed as a job of its own on a
debug partition, which is the only route that leaves the held allocation.

The declaration is carried the way a placement carries every resource request,
as a scheduler option string, so a node whose work needs more declares it
through the dispatch ``--set`` override and no key of its own is added.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from reckon import flight
from reckon._backends import LaunchPlan
from reckon.crew import placement

dispatch_launch = importlib.import_module("reckon.crew.dispatch_launch")

RESERVATION_ID = "1281970"
SRUN = "/opt/slurm/bin/srun"
SBATCH = "/opt/slurm/bin/sbatch"


def _plan() -> LaunchPlan:
    return LaunchPlan(
        backend="alpha",
        dialect="codex",
        argv=["/opt/backends/bin/codex", "exec", "--task", "t"],
        cwd="/work/tree",
        stdin_text="",
        environment={"PATH": "/opt/slurm/bin:/opt/backends/bin"},
        final_message_path=None,
        resumed_session=None,
    )


def _placed(placement_options, *, run_id=None, monkeypatch=None) -> list[str]:
    backend = {
        "launch": "cli",
        "command": "codex",
        "placement": {"scheduler": "srun", "options": list(placement_options)},
    }
    monkeypatch.setattr(flight, "placement_for", lambda _: backend["placement"])
    monkeypatch.setattr(
        placement, "ensure_reservation", lambda **_: {"job_id": RESERVATION_ID}
    )
    monkeypatch.setattr(
        placement, "read_reservation", lambda _: {"job_id": RESERVATION_ID}
    )
    monkeypatch.setattr(placement, "reservation_alive", lambda _: True)

    def which(name, *_, **__):
        return {"srun": SRUN, "sbatch": SBATCH}.get(name)

    monkeypatch.setattr(dispatch_launch.shutil, "which", which)
    plan = dispatch_launch.resolve_backend_placement(
        _plan(), backend, payload={"run_id": run_id} if run_id else None
    )
    return list(plan.argv)


def test_a_placed_step_carries_the_default_memory_bound(monkeypatch) -> None:
    """An undeclared worker still runs inside a bounded share."""
    argv = _placed([], monkeypatch=monkeypatch)

    assert f"--mem={placement.DEFAULT_STEP_MEMORY_GB}G" in argv
    # The step is still a step of the held allocation, not a job of its own.
    assert argv[:3] == [SRUN, "--overlap", f"--jobid={RESERVATION_ID}"]


def test_a_declared_bound_replaces_the_default(monkeypatch) -> None:
    """A bound declared through the placement reaches the placed step."""
    argv = _placed(
        ["--ntasks=1", placement.step_memory_option(48)], monkeypatch=monkeypatch
    )

    assert "--mem=48G" in argv
    assert f"--mem={placement.DEFAULT_STEP_MEMORY_GB}G" not in argv
    assert argv[:3] == [SRUN, "--overlap", f"--jobid={RESERVATION_ID}"]


def test_a_declaration_above_the_ceiling_leaves_the_allocation(monkeypatch) -> None:
    """A step too large for the allocation is placed as a job of its own.

    It is submitted with the batch client, the only one that starts a job of
    its own from inside a held allocation, on a debug partition, and the step
    client is not asked for a reservation step.
    """
    argv = _placed([placement.step_memory_option(64)], monkeypatch=monkeypatch)

    assert argv[0] == SBATCH
    assert f"--partition={placement.debug_partition()}" in argv
    assert placement.debug_partition().endswith(placement.DEBUG_PARTITION_SUFFIX)
    assert "--mem=64G" in argv
    assert not any(item.startswith("--jobid=") for item in argv)


def test_the_ceiling_is_reached_exactly_not_crossed_at_the_boundary() -> None:
    assert not placement.step_memory_above_ceiling(placement.STEP_MEMORY_CEILING_GB)
    assert placement.step_memory_above_ceiling(placement.STEP_MEMORY_CEILING_GB + 1)


def test_the_declaration_reaches_the_placement_through_a_dispatch_set(
    tmp_path: Path,
) -> None:
    """The bound flows through the existing ``--set`` override, not a new key."""
    shipped = tmp_path / "shipped.yaml"
    shipped.write_text(
        "version: 1\ndefault_backend: alpha\n"
        "backends:\n  alpha:\n    launch: cli\n    command: codex\n"
    )
    overrides = flight.parse_overrides(
        [
            "backends.alpha.placement.scheduler=srun",
            "backends.alpha.placement.options=['--ntasks=1','--mem=64G']",
        ]
    )
    resolved = flight.resolve(
        overrides=overrides,
        shipped_path=shipped,
        host_path=tmp_path / "absent-host.yaml",
    )

    declared = flight.placement_for(resolved.config["backends"]["alpha"])
    assert declared is not None
    assert placement.declared_memory_gb(declared["options"]) == 64


def test_a_bound_named_in_options_is_read_back() -> None:
    assert placement.declared_memory_gb(["--ntasks=1"]) is None
    assert placement.declared_memory_gb(["--mem=48G"]) == 48
    # A malformed bound is not read as zero, which would place an unbounded step.
    assert placement.declared_memory_gb(["--mem=big"]) is None


def test_the_step_prefix_is_the_owner_of_the_placement_prefix() -> None:
    """The fleet node's placement argv is composed by step_prefix, not beside it."""
    from reckon.crew import fleet_node

    job = {"jobid": RESERVATION_ID, "comment": fleet_node.FLEET_COMMENT}
    argv = fleet_node.placement_argv(job, ["hostname"])

    assert argv == [
        "srun",
        *placement.step_prefix(RESERVATION_ID, ["hostname"]),
    ]
