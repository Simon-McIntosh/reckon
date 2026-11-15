"""An srun placement with no job id ends when its client ends on its launch host.

An ``srun`` client lives exactly as long as the step it launched. A placement
whose job id was never declared cannot be asked of the scheduler, so before
this reading its end could never be observed: two runs sat at "liveness
unknown" with their client pids gone from their own launch host, and every
resume refused because nothing had observed the end it would rest on.
"""

from __future__ import annotations

import socket
from typing import Any

from reckon.crew import process_liveness

SRUN_PLACEMENT = {
    "job_id_status": "no-probe-declared",
    "options": ["--ntasks=1"],
    "scheduler": "srun",
    "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
    "reason_query": ["squeue", "-h", "-j", "{job}", "-o", "%R"],
}


def _record(**overrides: Any) -> dict[str, Any]:
    record = {
        "run_id": "r-placed",
        "pid": 424242,
        "launcher_host": socket.gethostname(),
        "placement": dict(SRUN_PLACEMENT),
        "job_id": None,
    }
    record.update(overrides)
    return record


def _scheduler_unasked(_record: Any) -> None:
    return None


def test_a_dead_srun_client_on_its_launch_host_reads_as_ended() -> None:
    reading = process_liveness.record_process_liveness(
        _record(), alive=lambda pid: False, job_alive=_scheduler_unasked
    )
    assert reading.alive is False


def test_a_live_srun_client_stays_unknown() -> None:
    """A running client is the scheduler's process, not proof the worker lives."""
    reading = process_liveness.record_process_liveness(
        _record(), alive=lambda pid: True, job_alive=_scheduler_unasked
    )
    assert reading.alive is None


def test_a_client_launched_on_another_host_stays_unknown() -> None:
    """This host's process table says nothing about another machine's pid."""
    reading = process_liveness.record_process_liveness(
        _record(launcher_host="some-other-node.example"),
        alive=lambda pid: False,
        job_alive=_scheduler_unasked,
    )
    assert reading.alive is None


def test_a_placement_with_a_job_id_is_still_asked_of_its_job() -> None:
    """A declared job is the authority; the client pid is not consulted."""
    asked: list[Any] = []

    def job_alive(record: Any) -> bool:
        asked.append(record.get("job_id"))
        return True

    reading = process_liveness.record_process_liveness(
        _record(job_id="1317001"), alive=lambda pid: False, job_alive=job_alive
    )
    assert asked == ["1317001"]
    assert reading.alive is True
