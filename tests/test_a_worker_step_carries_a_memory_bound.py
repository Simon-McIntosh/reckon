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

import pytest

from reckon import flight
from reckon._backends import LaunchPlan
from reckon.crew import placement

dispatch_launch = importlib.import_module("reckon.crew.dispatch_launch")
LaunchResolutionError = dispatch_launch.LaunchResolutionError

RESERVATION_ID = "1281970"
SRUN = "/opt/slurm/bin/srun"


def _plan(*, stdin_text: str = "", environment=None) -> LaunchPlan:
    return LaunchPlan(
        backend="alpha",
        dialect="codex",
        argv=["/opt/backends/bin/codex", "exec", "--task", "t"],
        cwd="/work/tree",
        stdin_text=stdin_text,
        environment=environment or {"PATH": "/opt/slurm/bin:/opt/backends/bin"},
        final_message_path=None,
        resumed_session=None,
    )


def _placed_plan(
    placement_options, *, run_id=None, monkeypatch=None, plan=None
) -> LaunchPlan:
    backend = {
        "launch": "cli",
        "command": "codex",
        "placement": {
            "scheduler": "srun",
            "partition": "all",
            "options": list(placement_options),
        },
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
        return {"srun": SRUN}.get(name)

    monkeypatch.setattr(dispatch_launch.shutil, "which", which)
    return dispatch_launch.resolve_backend_placement(
        _plan() if plan is None else plan,
        backend,
        payload={"run_id": run_id} if run_id else None,
    )


def _placed(placement_options, *, run_id=None, monkeypatch=None) -> list[str]:
    return list(
        _placed_plan(placement_options, run_id=run_id, monkeypatch=monkeypatch).argv
    )


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

    The step client is asked for an allocation of its own on a debug partition:
    named exactly once, never as a step of the fleet job, with the bound stated
    and a time limit that fits the window such a partition allows. The command
    is carried as arguments, so the worker's own standard streams are the ones
    that run rather than a batch output file's.
    """
    plan = _placed_plan(
        [placement.step_memory_option(64)], run_id="run-abc", monkeypatch=monkeypatch
    )
    argv = list(plan.argv)

    assert argv[0] == SRUN
    assert argv.count(SRUN) == 1
    assert not any(token.startswith("--jobid=") for token in argv)
    assert "--wrap" not in argv
    assert f"--partition={placement.debug_partition()}" in argv
    assert placement.debug_partition().endswith(placement.DEBUG_PARTITION_SUFFIX)
    assert "--mem=64G" in argv
    assert "--job-name=run-abc" in argv
    # A limit is always stated, and never beyond the window the partition allows.
    limit = next(token for token in argv if token.startswith("--time="))
    assert limit <= f"--time={placement.DEBUG_JOB_TIME_CEILING}"
    # The command is the tail of the invocation, as argv rather than a script.
    assert argv[-len(_plan().argv) :] == list(_plan().argv)
    # The allocation's own identity is gone, so the client cannot read itself as
    # a step of the fleet job; the rest of the environment is untouched.
    environment = plan.environment
    assert environment["PATH"] == _plan().environment["PATH"]
    assert not any(
        str(key).upper().startswith(placement.SLURM_JOB_ENVIRONMENT_ROOTS)
        for key in environment
    )


def test_a_plan_above_the_ceiling_keeps_the_prompt_it_carries(monkeypatch) -> None:
    """A worker placed as a job of its own is still given the same prompt.

    Every dialect writes the worker's prompt to the launched process's stdin, so
    a route that emptied it would start a worker with nothing to do. The
    over-ceiling route leaves it alone: the client forwards stdin to its single
    task exactly as a step of the allocation does.
    """
    plan = _plan(stdin_text="the prompt a worker is given")
    placed = _placed_plan(
        [placement.step_memory_option(64)], monkeypatch=monkeypatch, plan=plan
    )

    assert placed.stdin_text == plan.stdin_text
    assert placed.argv[0] == SRUN
    # The command is carried as argv, so the prompt is not folded into it.
    assert placed.argv[-len(plan.argv) :] == list(plan.argv)


def test_a_job_of_its_own_states_a_limit_inside_the_debug_window() -> None:
    """A debug partition allows an hour and has no default, so a limit is stated."""
    assert placement.debug_job_time_limit(None) == placement.DEBUG_JOB_TIME_CEILING
    assert placement.debug_job_time_limit("8h") == placement.DEBUG_JOB_TIME_CEILING
    assert (
        placement.debug_job_time_limit("01:00:00") == placement.DEBUG_JOB_TIME_CEILING
    )
    assert placement.debug_job_time_limit("30m") == "00:30:00"
    assert (
        placement.debug_job_time_limit("unreadable") == placement.DEBUG_JOB_TIME_CEILING
    )


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


def test_one_parser_reads_a_memory_figure_in_both_directions() -> None:
    """A declared bound and a reported size are read by the same parser.

    The placement declares a step's memory and the fleet node reads back the
    memory the scheduler reports for an allocation. Both are the same vocabulary
    stated unconnectedly, so the two answers must agree rather than merely look
    alike, and each caller keeps the unit it publishes: bytes on one side, whole
    gigabytes on the other.
    """
    from reckon.crew import fleet_node

    for figure in ("96G", "98304M", "98304", "1T", "512M"):
        total = placement.memory_figure_bytes(figure)
        assert total is not None, figure
        assert fleet_node.parse_memory_gb(figure) == total // (1024**3), figure
    # The units are the scheduler's, so a bare number is the megabytes it means.
    assert fleet_node.parse_memory_gb("98304M") == 96
    assert fleet_node.parse_memory_gb("96G") == 96
    assert placement.memory_figure_bytes("98304") == 98304 * 1024**2
    # The reported-size reader drops the per-node/per-cpu suffix the scheduler
    # prints; the figure itself is still read by the shared parser.
    assert fleet_node.parse_memory_gb("96Gn") == 96
    assert fleet_node.parse_memory_gb("unreadable") is None


def test_a_bound_named_in_options_is_read_back() -> None:
    assert placement.declared_memory_gb(["--ntasks=1"]) is None
    assert placement.declared_memory_gb(["--mem=48G"]) == 48
    # A malformed bound is not read as zero, which would place an unbounded step.
    assert placement.declared_memory_gb(["--mem=big"]) is None


def test_both_spellings_of_a_bound_read_the_same() -> None:
    """A placement writes an option joined or separated; both name one bound."""
    joined = placement.step_prefix("1281970", ["--mem=64G"])
    separated = placement.step_prefix("1281970", ["--mem", "64G"])

    # The declared spelling is placed unchanged, and neither gains a default.
    assert joined == ["--overlap", "--jobid=1281970", "--mem=64G"]
    assert separated == ["--overlap", "--jobid=1281970", "--mem", "64G"]
    assert placement.declared_memory_gb(["--mem", "64G"]) == 64
    assert placement.declared_memory_bytes(["--mem", "64G"]) == 64 * 1024**3


def test_a_declared_bound_is_never_restated_beside_itself() -> None:
    """No step is given a default beside the bound its placement declared.

    Two memory requests in one argv are a conflict rather than a bound: the
    default would compete with the declaration it was meant to defer to.
    """
    options = ["--ntasks=1", "--mem", "64G"]
    placed = placement.step_prefix("1281970", options)
    argv = ["srun", *placed, "/opt/backends/bin/codex", "exec"]

    assert sum(item.startswith("--mem") for item in argv) == 1
    assert f"--mem={placement.DEFAULT_STEP_MEMORY_GB}G" not in argv


def test_a_step_per_cpu_bound_is_totalled_from_the_task_count() -> None:
    """A per-task figure is the placement's own task count multiplied by it."""
    options = ["--cpus-per-task=16", "--mem-per-cpu", "4G"]

    assert placement.declared_memory_gb(options) == 64
    assert placement.step_memory_above_ceiling(placement.declared_memory_gb(options))
    # The step states its bound once, in the spelling declared.
    assert placement.step_prefix("1281970", options)[2:] == options


def test_a_per_cpu_bound_with_no_task_count_is_refused(monkeypatch) -> None:
    """A total that cannot be read is refused rather than priced as one task."""
    options = ["--mem-per-cpu=4G"]

    assert placement.declared_memory_gb(options) is None
    assert placement.unresolved_memory_declaration(options) == "--mem-per-cpu=4G"
    with pytest.raises(LaunchResolutionError):
        _placed(options, monkeypatch=monkeypatch)


def test_the_step_prefix_is_the_owner_of_the_placement_prefix() -> None:
    """The fleet node's placement argv is composed by step_prefix, not beside it."""
    from reckon.crew import fleet_node

    job = {"jobid": RESERVATION_ID, "comment": fleet_node.FLEET_COMMENT}
    argv = fleet_node.placement_argv(job, ["hostname"])

    assert argv == [
        "srun",
        *placement.step_prefix(RESERVATION_ID, ["hostname"]),
    ]
