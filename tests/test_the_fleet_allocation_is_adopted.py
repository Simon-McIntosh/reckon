"""The fleet adopts the allocation it already runs under.

The unified allocation is the supervisor-running kind: a whole-node batch job
whose batch step runs ``fleet-supervisor`` and publishes the fleet record. That
job is the one every session should place into, so the ensure command must adopt
it rather than mint a second allocation beside it. These cases pin that: an
adoption from a recorded supervisor-running job submits nothing and publishes
that job with the shape the scheduler reports for it; a second project reads the
same published job and submits nothing; a resumed run of a placement-declaring
backend becomes a step inside the adopted job; and a backend declaring no
placement is launched on resume exactly as before.

The scheduler is a recording stub on the PATH throughout — no allocation is
submitted, adopted, cancelled or reloaded for real, and the property under test
is reckon's plumbing: which job is published and how many allocation-obtaining
requests are made. The shared crew state and the fleet state are both temporary
homes, so no case touches the ambient configuration or fleet record.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import (
    placement,
    resumption,  # noqa: F401 - in the node's declared population
)
from tests import test_a_live_run_never_reads_dead as liveness

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The supervisor-running allocation, and the shape the recording scheduler
# reports for it. The shape is deliberately unlike the module's declared
# defaults (rigel/26/96G), so a record carrying it proves the size was read from
# the scheduler rather than taken from reckon's own constants.
ADOPTED_ID = "55600002"
ADOPTED_PARTITION = "rigel"
ADOPTED_CORES = 40
ADOPTED_MEMORY_GB = 128
ADOPTED_ROW = (
    f"{ADOPTED_ID}|reckon-fleet|RUNNING|00:10:00|clu-2018||reckon-fleet|UNLIMITED"
)
ADOPTED_SHAPE = f"{ADOPTED_PARTITION}|{ADOPTED_CORES}|{ADOPTED_MEMORY_GB}G"

# A backend that declares a shared-allocation placement, and one that declares
# none. The resumed run of each is what the last two cases compare.
CONFIG = {
    "backends": {
        "alpha": {"launch": "cli", "command": "clive"},
        "beta": {
            "launch": "cli",
            "command": "clive",
            "placement": {"scheduler": "srun", "options": ["--partition=all"]},
        },
    }
}

# Every stub appends its own argv to this log, so a case shows which questions
# were asked and how many allocations were obtained. squeue answers the four
# query shapes the placement code asks: the job listing, the shape probe, the
# state probe and the pending-reason probe.
_RECORDING_SCHEDULER = """#!/bin/sh
name=${0##*/}
printf '%s\\t%s\\n' "$name" "$*" >> "$FAKE_SCHEDULER_LOG"
case "$name" in
  salloc)
    printf 'salloc: Granted job allocation %s\\n' "${FAKE_JOB:-1274051}"
    ;;
  sbatch)
    printf '%s\\n' "${FAKE_SBATCH_JOB:-99000001}"
    ;;
  squeue)
    args="$*"
    case "$args" in
      *%i*) printf '%s\\n' "${FAKE_JOB_ROW:-__ROW__}" ;;
      *%P*) printf '%s\\n' "${FAKE_SHAPE:-__SHAPE__}" ;;
      *%T*) printf '%s\\n' "${FAKE_STATE:-RUNNING}" ;;
      *%R*) printf '%s\\n' "None" ;;
      *) : ;;
    esac
    ;;
esac
exit 0
""".replace("__ROW__", ADOPTED_ROW).replace("__SHAPE__", ADOPTED_SHAPE)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point crew state and fleet state at temporary homes before any write."""
    home = tmp_path / "config"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("FLEET_STATE_DIR", str(tmp_path / "fleet-state"))
    monkeypatch.setenv("FAKE_SCHEDULER_LOG", str(home / "scheduler.log"))
    return home


def _recording_scheduler(directory: Path) -> Path:
    """Executables named for the scheduler verbs, each recording its own argv."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("salloc", "sbatch", "squeue", "srun", "clive"):
        executable = directory / name
        executable.write_text(_RECORDING_SCHEDULER, encoding="utf-8")
        executable.chmod(0o755)
    return directory


def _scheduler_path(bin_dir: Path) -> str:
    """The recording stubs first on the system PATH, so they shadow any client."""
    return os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])


def _asked(name: str) -> list[str]:
    """The recorded invocations of one scheduler verb."""
    log = Path(os.environ["FAKE_SCHEDULER_LOG"])
    if not log.exists():
        return []
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(name + "\t")
    ]


def _allocation_requests() -> int:
    """Requests that ask the scheduler for an allocation: salloc or sbatch."""
    return len(_asked("salloc")) + len(_asked("sbatch"))


def _publish_fleet_record(tmp_path: Path) -> None:
    """Write the fleet record naming the supervisor-running allocation."""
    directory = tmp_path / "fleet-state"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "record.json").write_text(
        json.dumps({"job_id": ADOPTED_ID, "node": "clu-2018"}),
        encoding="utf-8",
    )


def _step_job_id(argv: list[str]) -> str | None:
    """The allocation id a placed worker's argv declares, or None."""
    ids = [
        item.removeprefix("--jobid=") for item in argv if item.startswith("--jobid=")
    ]
    assert len(ids) <= 1, f"expected at most one --jobid in {argv}"
    return ids[0] if ids else None


def _resumable_pointer(home: Path, *, backend: str, run_id: str) -> None:
    """A live pointer whose run the supervisor recorded as ended, so it resumes."""
    tree = home / "trees" / run_id
    tree.mkdir(parents=True, exist_ok=True)
    ledger_root = home / "ledger"
    ledger_root.mkdir(parents=True, exist_ok=True)
    manifest = crew.run_dir(run_id) / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {run_id}\nstatus: in-progress\n")
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "repo": str(ledger_root),
            "worktree": str(tree),
            "launch": "cli",
            "backend": backend,
            "sandbox": "worktree-full",
            "session_id": "sess-recorded-on-the-pointer",
            "manifest_path": str(manifest),
            "argv": ["clive", "-p", "--output-format", "stream-json", "--verbose"],
            "agent": {"launch": "cli", "backend": backend},
        },
    )
    liveness._write_exit_record(run_id)


def test_a_live_supervisor_allocation_is_adopted_without_submitting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A recorded supervisor-running job is adopted; nothing is obtained.

    With no published reservation and a fleet record naming a live allocation,
    the ensure publishes that allocation's own job id with the shape the
    scheduler reports for it, and makes no allocation-obtaining request.
    """
    _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    _publish_fleet_record(tmp_path)

    result = placement.ensure_reservation(project="alpha", session="s-1")

    # The count is asserted first, so this case fails on the very thing adoption
    # removes — an allocation-obtaining request — rather than on a later field.
    assert _allocation_requests() == 0, "adoption submits nothing"
    assert result["reason"] == "adopted"
    assert result["adopted"] is True
    assert result["started"] is False
    assert result["job_id"] == ADOPTED_ID

    record = placement.read_reservation()
    assert record is not None
    assert record["job_id"] == ADOPTED_ID
    # The shape is the scheduler's, not the module's declared defaults.
    assert record["partition"] == ADOPTED_PARTITION
    assert record["size"] == {"cores": ADOPTED_CORES, "memory_gb": ADOPTED_MEMORY_GB}


def test_a_second_project_reads_the_adopted_job_without_submitting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The adopted job is shared: a second project resolves it and starts nothing."""
    _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    _publish_fleet_record(tmp_path)

    first = placement.ensure_reservation(project="alpha", session="s-1")
    second = placement.ensure_reservation(project="beta", session="s-2")

    assert first["job_id"] == second["job_id"] == ADOPTED_ID
    assert second["reason"] == "already-held"
    assert _allocation_requests() == 0, "the second project submits nothing"
    assert placement.read_reservation("beta")["job_id"] == ADOPTED_ID


def test_a_resumed_run_is_placed_as_a_step_in_the_adopted_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resumed run of a placed backend becomes a step in the adopted job.

    The resume path resolves placement exactly as a dispatch does: the resumed
    worker is an overlapping step whose argv carries the adopted job's id, and
    no allocation is obtained to launch it.
    """
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    _publish_fleet_record(tmp_path)
    run_id = "r-resume-into-adopted-job"
    _resumable_pointer(home, backend="beta", run_id=run_id)

    plan = dispatch_module.resume_plan(run_id, "continue", config=CONFIG)

    assert Path(plan.argv[0]).name == "srun"
    assert "--overlap" in plan.argv
    assert _step_job_id(plan.argv) == ADOPTED_ID
    assert _allocation_requests() == 0, "a resume places into the held allocation"
    assert placement.read_reservation()["job_id"] == ADOPTED_ID


def test_a_backend_declaring_no_placement_is_untouched_on_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No placement means no step: the resumed worker launches as it always did."""
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    _publish_fleet_record(tmp_path)
    run_id = "r-resume-unplaced"
    _resumable_pointer(home, backend="alpha", run_id=run_id)

    plan = dispatch_module.resume_plan(run_id, "continue", config=CONFIG)

    assert "srun" not in [Path(item).name for item in plan.argv]
    assert _step_job_id(plan.argv) is None
    assert _allocation_requests() == 0
    assert _asked("squeue") == [], "an unplaced backend asks the scheduler nothing"
