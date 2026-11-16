"""A worker step states the reservation's CPU width rather than inheriting one.

A step client reads ``SLURM_CPUS_PER_TASK`` and ``SLURM_TRES_PER_TASK`` from
its environment when its command line names no CPU count, and the admission
service that launches workers runs inside the fleet job, whose own per-task
figure is not the reservation's. When the fleet job asks for more CPUs per task
than the reservation holds, every inherited step asks for more processors than
the reservation permits and the scheduler refuses to create it, so no worker
launches. Stating the reservation's own width on the step's command line
overrides the inherited figure, and the width is the whole reservation so the
worker keeps the affinity an unconstrained step had: a step stated at one CPU
would be pinned to a single core.
"""

from __future__ import annotations

import importlib

from reckon._backends import LaunchPlan
from reckon import flight
from reckon.crew import placement

dispatch_launch = importlib.import_module("reckon.crew.dispatch_launch")

RESERVATION_ID = "1319171"
SRUN = "/opt/slurm/bin/srun"


def _placed(placement_options, reservation, monkeypatch) -> list[str]:
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
    monkeypatch.setattr(placement, "ensure_reservation", lambda **_: reservation)
    monkeypatch.setattr(placement, "read_reservation", lambda _: reservation)
    monkeypatch.setattr(placement, "reservation_alive", lambda _: True)
    monkeypatch.setattr(
        dispatch_launch.shutil,
        "which",
        lambda name, *_, **__: SRUN if name == "srun" else None,
    )
    plan = LaunchPlan(
        backend="alpha",
        dialect="codex",
        argv=["/opt/backends/bin/codex", "exec"],
        cwd="/work/tree",
        stdin_text="",
        environment={"PATH": "/opt/slurm/bin"},
        final_message_path=None,
        resumed_session=None,
    )
    return list(dispatch_launch.resolve_backend_placement(plan, backend).argv)


def _step_options(argv: list[str]) -> list[str]:
    # The step's own options sit between the scheduler and its task wrapper.
    return argv[1 : argv.index("/bin/sh")]


def test_a_step_states_the_width_the_reservation_was_held_at(monkeypatch) -> None:
    reservation = {
        "job_id": RESERVATION_ID,
        "options": ["--no-shell", "--cpus-per-task=24", "--mem=128G"],
    }
    options = _step_options(_placed([], reservation, monkeypatch))

    assert placement.option_values(options, ("--cpus-per-task", "-c")) == [
        ("--cpus-per-task", "24")
    ]


def test_a_record_naming_no_width_falls_back_to_the_held_size(monkeypatch) -> None:
    options = _step_options(_placed([], {"job_id": RESERVATION_ID}, monkeypatch))

    assert f"--cpus-per-task={placement.RESERVATION_CORES}" in options


def test_a_declared_cpu_count_is_kept_and_not_doubled(monkeypatch) -> None:
    reservation = {"job_id": RESERVATION_ID, "options": ["--cpus-per-task=32"]}
    options = _step_options(_placed(["-c", "4"], reservation, monkeypatch))

    assert placement.option_values(options, ("--cpus-per-task", "-c")) == [("-c", "4")]
