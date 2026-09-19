"""A backend may declare a placement; its launch is wrapped, not rebuilt.

The fault: every worker was a child of whoever started the coordinator, so the
fleet's ceiling was one login slice. A backend that declares a placement has its
already-resolved argv prefixed with the scheduler invocation, which is what keeps
the absolute executable, the environment and the stream paths the launch was
built with. A backend declaring none is launched exactly as it was before, which
is the sense in which the field is optional — absence is not an empty placement
and changes nothing.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from reckon import flight
from reckon._backends import LaunchPlan

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RESOLVED_EXECUTABLE = "/opt/backends/bin/codex"


def _plan(argv: list[str] | None = None, environment: dict | None = None) -> LaunchPlan:
    return LaunchPlan(
        backend="alpha",
        dialect="codex",
        argv=list(argv or [RESOLVED_EXECUTABLE, "exec", "--task", "t"]),
        cwd="/work/tree",
        stdin_text="",
        environment=environment or {"PATH": "/usr/bin:/bin"},
        final_message_path=None,
        resumed_session=None,
    )


def _placed(**placement) -> dict:
    return {
        "launch": "cli",
        "command": "codex",
        "placement": {"scheduler": "srun", **placement},
    }


def test_a_declared_placement_prefixes_the_resolved_launch() -> None:
    backend = _placed(options=["--partition=all", "--job-name=node"])
    plan = dispatch_module.apply_backend_placement(_plan(), backend)

    assert plan.argv[:3] == ["/usr/bin/srun", "--partition=all", "--job-name=node"]
    assert plan.argv[3:] == [RESOLVED_EXECUTABLE, "exec", "--task", "t"]
    # The resolved executable is what runs, so the launch is not downgraded to a
    # bare name that only resolves on the compute node's PATH.
    assert Path(plan.argv[3]).is_absolute()
    assert plan.cwd == "/work/tree"
    assert plan.environment == {"PATH": "/usr/bin:/bin"}


def test_the_launch_is_carried_through_rather_than_replaced() -> None:
    backend = _placed(options=["--partition=all"])
    before = _plan()
    after = dispatch_module.apply_backend_placement(before, backend)

    assert after.argv[-len(before.argv) :] == before.argv
    assert after.dialect == before.dialect
    assert after.backend == before.backend
    assert after.stdin_text == before.stdin_text


def test_a_backend_declaring_no_placement_launches_as_before() -> None:
    before = _plan()
    unchanged = dispatch_module.apply_backend_placement(
        before, {"launch": "cli", "command": "codex"}
    )

    assert unchanged.argv == before.argv
    assert unchanged == before


def test_an_unresolvable_scheduler_is_refused_before_launch() -> None:
    backend = {
        "launch": "cli",
        "command": "codex",
        "placement": {"scheduler": "srun", "options": ["--partition=all"]},
    }
    plan = _plan(environment={"PATH": "/nonexistent-scheduler-bin"})
    with pytest.raises(dispatch_module.LaunchResolutionError) as refused:
        dispatch_module.apply_backend_placement(plan, backend)

    message = str(refused.value)
    assert "srun" in message
    assert "/nonexistent-scheduler-bin" in message
    assert "nothing has been launched" in message


def test_a_placement_that_names_no_scheduler_is_refused() -> None:
    with pytest.raises(flight.FlightConfigError) as refused:
        flight.validate_layer(
            {"backends": {"alpha": {"launch": "cli", "placement": {"options": []}}}},
            "probe.yaml",
        )
    assert "backends.alpha.placement.scheduler" in str(refused.value)


def test_a_placement_field_outside_the_declared_set_is_refused() -> None:
    # The scheduler is named, so the misspelling is the only defect and the
    # refusal has to be the unknown key rather than a missing required one.
    with pytest.raises(flight.FlightConfigError) as refused:
        flight.validate_layer(
            {
                "backends": {
                    "alpha": {
                        "launch": "cli",
                        "placement": {"scheduler": "srun", "schduler": "srun"},
                    }
                }
            },
            "probe.yaml",
        )
    assert "schduler" in str(refused.value)


def test_a_declared_placement_survives_resolution(tmp_path: Path) -> None:
    shipped = tmp_path / "shipped.yaml"
    shipped.write_text(
        "version: 1\ndefault_backend: alpha\n"
        "backends:\n  alpha:\n    launch: cli\n    command: codex\n"
    )
    resolved = flight.resolve(
        overrides={
            "backends": {
                "alpha": {
                    "placement": {
                        "scheduler": "srun",
                        "options": ["--partition=all"],
                    }
                }
            }
        },
        shipped_path=shipped,
        host_path=tmp_path / "absent-host.yaml",
    )

    placement = flight.placement_for(resolved.config["backends"]["alpha"])
    assert placement is not None
    assert placement["scheduler"] == "srun"
    assert placement["options"] == ["--partition=all"]


def test_the_job_identifier_is_read_from_the_declared_probe() -> None:
    placement = {
        "scheduler": "srun",
        "options": [],
        "job_id_probe": ["squeue", "--me", "--name={run}", "--noheader"],
    }
    seen: list[list[str]] = []

    def runner(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="1274052\n", stderr="")

    job_id, status = dispatch_module.placement_job_id(
        placement, run_id="r-1", runner=runner
    )

    assert job_id == "1274052"
    assert status == "recorded"
    assert seen == [["squeue", "--me", "--name=r-1", "--noheader"]]


def test_a_probe_answering_no_identifier_records_none() -> None:
    placement = {"scheduler": "srun", "options": [], "job_id_probe": ["squeue"]}

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no job")

    job_id, status = dispatch_module.placement_job_id(
        placement, run_id="r-1", runner=runner
    )

    assert job_id is None
    assert "no identifier" in status


def test_an_unplaced_backend_records_no_job_identity() -> None:
    job_id, status = dispatch_module.placement_job_id(None, run_id="r-1")

    assert job_id is None
    assert status == "no-placement"
