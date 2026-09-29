"""The ensure holds one shared allocation, once, even under a cross-process race.

A placement reservation is one allocation held for the whole workstation, so
the operation that holds it must be idempotent across separate dispatching
processes rather than merely within one. These cases pin that: four processes
that race from no record produce one allocation between them, a probe that
cannot be completed is unknown and never read as an absent reservation, a live
allocation is never replaced, and a released one is replaced once.

The scheduler is a recording fake on the PATH throughout — the property under
test is how many allocations are asked for, not what a real cluster does with
them — and the shared crew state is a temporary home, so no case reaches the
ambient config home or a real allocation. The recorded allocation the probe
acts on is obtained and found through the fleet node's own hold path
(generate_hold_script, submit, find_allocation), which is the one hold path
the codebase carries.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import fleet_node, placement

# A job id recorded as held, and a distinct one a replacement grants.
HELD_ID = "99000001"
NEW_ID = "1274099"

# Every fake scheduler name appends its own argv to this log; the log is how a
# case shows which questions were asked and which allocations were obtained.
_RECORDING_SCHEDULER = """#!/bin/sh
name=${0##*/}
printf '%s\\t%s\\n' "$name" "$*" >> "$FAKE_SCHEDULER_LOG"
case "$name" in
  salloc)
    sleep "${FAKE_SLEEP:-0}"
    printf 'salloc: Granted job allocation %s\\n' "${FAKE_JOB:-1274051}"
    ;;
  sbatch)
    printf '%s\\n' "${FAKE_SBATCH_JOB:-99000001}"
    ;;
  squeue)
    printf '%s\\n' "${FAKE_SQUEUE_STATE-RUNNING}"
    ;;
esac
exit "${FAKE_SCHEDULER_EXIT:-0}"
"""

# The race the lock must settle. The child waits on a gate so all four
# processes arrive at the ensure together, then prints the job id it got, so
# the parent can show all four reached the same allocation.
_CHILD = """
import os
import time
from pathlib import Path

gate = Path(os.environ["FAKE_GATE"])
while not gate.exists():
    time.sleep(0.005)
from reckon.crew import placement

print(placement.ensure_reservation()["job_id"])
"""


def _recording_scheduler(directory: Path) -> Path:
    """Executables named for the scheduler verbs, each recording its own argv."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("salloc", "sbatch", "squeue", "srun"):
        executable = directory / name
        executable.write_text(_RECORDING_SCHEDULER, encoding="utf-8")
        executable.chmod(0o755)
    return directory


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the shared crew state at a temporary home."""
    home = tmp_path / "config"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("FAKE_SCHEDULER_LOG", str(home / "scheduler.log"))
    return home


def _scheduler_path(bin_dir: Path) -> str:
    """The recording stubs first on the system PATH.

    The stubs stand in for the scheduler verbs, so they must shadow any real
    client; the rest of the system PATH is kept behind them so ordinary shell
    tools the stubs use still resolve. A stub run against a PATH holding
    nothing but itself cannot find ``sleep``, and its error on stderr would
    then be scanned by the submitter's job-id reader alongside the allocation
    it did grant.
    """
    return os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])


def _asked(name: str) -> list[str]:
    """The recorded invocations of one scheduler verb."""
    log = Path(os.environ["FAKE_SCHEDULER_LOG"])
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.startswith(name + "\t")]


def _held_through_the_fleet_node(tmp_path: Path) -> str:
    """Hold an allocation through the fleet node's own path, and return its id.

    The recorded allocation the probe acts on is obtained from the hold script
    and submitter the fleet node already carries, and resolved from its job
    comment through ``find_allocation``, rather than being fabricated as a bare
    job id: there is one hold path in the codebase and this exercises it.
    """
    script = fleet_node.generate_hold_script(
        fleet_node.fleet_size(), log_path=tmp_path / "fleet-%j.log"
    )
    job_id = fleet_node.submit(script)
    rows = [
        {
            "jobid": job_id,
            "name": "reckon-fleet",
            "state": "RUNNING",
            "time": "00:01:00",
            "node": "clu-2018",
            "gres": "",
            "comment": "reckon-fleet",
        }
    ]
    found = fleet_node.find_allocation(rows, preferred=job_id)
    assert found is not None
    return found["jobid"]


def test_four_processes_racing_from_no_record_hold_one_allocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The property the cross-process lock exists for; one process cannot show it.

    Four separate processes call the ensure together. If the claim were held
    only inside one process each would read no record, conclude the reservation
    absent, and mint an allocation of its own, so the recording scheduler would
    log four. With the claim held across processes exactly one submits, the
    other three block and then read the record it published, and all four
    return that one job id.
    """
    import sys

    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    gate = tmp_path / "gate"
    environment = {
        **os.environ,
        "PATH": _scheduler_path(bin_dir),
        "RECKON_HOME": str(home),
        "FAKE_SCHEDULER_LOG": str(home / "scheduler.log"),
        "FAKE_GATE": str(gate),
        "FAKE_SLEEP": "0.4",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    gate.write_text("go\n", encoding="utf-8")
    outputs = []
    for child in children:
        stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr
        outputs.append(stdout.strip().splitlines()[-1])

    assert len(_asked("salloc")) == 1
    assert len(set(outputs)) == 1, outputs
    assert outputs[0] == placement.read_reservation()["job_id"]


def test_a_probe_that_cannot_be_completed_is_unknown_and_submits_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unknown must not read as absent, so the ensure waits and retries.

    A recorded allocation whose scheduler cannot be asked about the job leaves
    the question unanswered, not answered in the negative. The ensure asks
    again while the answer stays unknown and submits nothing throughout, then
    reports the unknown once its bound is reached — where reading the silence
    as an absent reservation would mint a second allocation beside the one the
    record names.
    """
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": HELD_ID})
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    # A non-zero exit is a question that could not be asked, distinct from a
    # query that ran and named no job.
    monkeypatch.setenv("FAKE_SCHEDULER_EXIT", "1")

    result = placement.ensure_reservation(session="s-1")

    assert result["started"] is False
    assert result["job_id"] == HELD_ID
    assert result["probe"] == "unknown"
    assert _asked("salloc") == [], "nothing may be obtained while the probe is unknown"
    # It waited and asked again rather than reading the first silence as absent.
    assert len(_asked("squeue")) == placement._UNKNOWN_PROBE_ATTEMPTS
    assert placement.read_reservation()["job_id"] == HELD_ID


def test_a_recorded_allocation_reported_alive_is_never_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live allocation is reported and no allocation is obtained."""
    _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    held = _held_through_the_fleet_node(tmp_path)
    placement.publish_reservation({"job_id": held})
    monkeypatch.setenv("FAKE_SQUEUE_STATE", "RUNNING")
    monkeypatch.setenv("FAKE_SCHEDULER_EXIT", "0")

    result = placement.ensure_reservation(session="s-1")

    assert result["started"] is False
    assert result["reason"] == "already-held"
    assert result["job_id"] == held
    assert _asked("salloc") == [], "a live allocation is not replaced"


def test_a_recorded_allocation_reported_gone_is_replaced_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A released allocation is replaced exactly once, under the one hold path."""
    _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    held = _held_through_the_fleet_node(tmp_path)
    placement.publish_reservation({"job_id": held})
    # A successful query that names no job: the scheduler knows it not, so it
    # has left the queue and the record must be replaced.
    monkeypatch.setenv("FAKE_SQUEUE_STATE", "")
    monkeypatch.setenv("FAKE_SCHEDULER_EXIT", "0")
    monkeypatch.setenv("FAKE_JOB", NEW_ID)

    result = placement.ensure_reservation(session="s-1")

    assert result["started"] is True
    assert result["job_id"] == NEW_ID
    assert result["record"]["replaced"] == held
    assert len(_asked("salloc")) == 1
    assert placement.read_reservation()["job_id"] == NEW_ID
