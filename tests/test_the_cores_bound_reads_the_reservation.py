"""The cores bound reads the shared reservation's admitted size, not --ntasks.

A placement's scheduler options describe the step a worker runs as, not the
size of the allocation it runs inside: ``--ntasks=1`` is the shape of one
step. Reading it as the admitted cores made a reservation of hundreds of cores
refuse every dispatch after the first, because the established reservations
declare ``--ntasks=1`` and the bound then read one core. The admitted size lives
in the reservation's own published record instead, and the bound is measured
against the workers actually placed inside it rather than every live pointer on
the backend.

Every case is hermetic: ``RECKON_HOME`` moves the crew directory into a temp
tree, the reservation record and the live pointers are synthesised there, and no
scheduler verb is run. Each placed pointer records a live pid — this process —
so ``occupying_the_reservation`` counts it, exactly as it counts a worker still
holding memory in the real allocation.
"""

from __future__ import annotations

import json
import os

import pytest

from reckon import crew
from reckon.crew import placement as placement_module
from reckon.crew import summary as summary_module
from reckon.crew.dispatch import _refuse_over_concurrency_ceiling

# The measured case: a clive placement whose only step option is --ntasks=1,
# running inside a reservation of 28 cores.
CLIVE = {"placement": {"scheduler": "srun", "options": ["--ntasks=1"]}}

RESERVATION_CORES = 28


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _publish_reservation(*, cores: int = RESERVATION_CORES, job_id: str = "88000001"):
    """Publish the shared reservation record the bound now reads its size from."""
    path = placement_module.reservation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "scheduler": "salloc",
                "step_scheduler": "srun",
                "size": {"cores": cores, "memory_gb": 120},
                "partition": "all",
            }
        ),
        encoding="utf-8",
    )
    return path


def _seed_live(run_id: str, *, placement: dict | None, backend: str = "clive") -> None:
    """Write one live pointer, placed with a live pid when a placement is given."""
    pointer = {
        "run_id": run_id,
        "project": "proj",
        "backend": backend,
        "phase": "working",
        "node": {"id": f"peer-{run_id}", "write_paths": []},
    }
    if placement is not None:
        pointer["placement"] = placement
        pointer["pid"] = os.getpid()
    crew._write_json(crew.pointer_path(run_id), pointer)


def _refusal_message(backend: dict) -> str:
    with pytest.raises(crew.CrewError) as excinfo:
        _refuse_over_concurrency_ceiling("clive", backend)
    return str(excinfo.value)


def test_the_measured_clive_case_is_admitted(home):
    """Twenty live clive pointers, one placed live worker, a 28-core reservation.

    The measured refusal read ``--ntasks=1`` as the admitted size and refused
    with "20 of 1 cores admitted". The reserved cores are 28, only one worker is
    placed inside them, so the dispatch fits and must be admitted.
    """
    _publish_reservation()
    options = CLIVE["placement"]
    for index in range(19):
        _seed_live(f"r-20261001T00000000000{index:02d}-unplaced-{index}", placement=None)
    _seed_live("r-20261001T0000000000099-placed", placement=options)

    # The bound the whole case is about: it admits, at the reservation's size.
    report = summary_module.fleet_bound_report(
        CLIVE, occupancy=20, reservation_occupancy=1
    )
    bound = next(row for row in report["bounds"] if row["name"] == "partition-cores")
    assert bound["admits_one_more"] is True
    assert bound["value"] == "1 of 28 cores admitted (1 per worker)"

    # And the dispatch entry point agrees rather than refusing the measured case.
    _refuse_over_concurrency_ceiling("clive", CLIVE)


def test_twenty_eight_placed_workers_are_refused_naming_the_reservation(home):
    """Twenty-eight placed live workers fill a 28-core reservation and refuse."""
    _publish_reservation()
    options = CLIVE["placement"]
    for index in range(RESERVATION_CORES):
        _seed_live(f"r-20261001T0000000000{index:02d}-placed-{index}", placement=options)

    message = _refusal_message(CLIVE)
    assert "partition admitted cores" in message
    assert "28 of 28 cores admitted (1 per worker)" in message


def test_a_four_core_worker_share_refuses_at_seven_and_admits_at_six(home):
    """--cpus-per-task=4 makes seven workers overshoot 28 cores and six fit."""
    _publish_reservation()
    options = {"scheduler": "srun", "options": ["--ntasks=1", "--cpus-per-task=4"]}
    backend = {"placement": options}

    for index in range(7):
        _seed_live(f"r-20261001T0000000000{index:02d}-seven-{index}", placement=options)
    message = _refusal_message(backend)
    assert "partition admitted cores" in message
    assert "28 of 28 cores admitted (4 per worker)" in message

    # One fewer worker fits inside the same 28 admitted cores.
    crew.pointer_path("r-20261001T000000000006-seven-6").unlink()
    _refuse_over_concurrency_ceiling("clive", backend)


def test_an_absent_reservation_leaves_the_cores_bound_unknown(home):
    """With no reservation record the admitted size is unstated, so it admits.

    An unstated size is not a zero: the bound reports unknown and refuses
    nothing, because a reservation that was never read cannot justify refusing
    work.
    """
    assert not placement_module.reservation_path().exists()
    for index in range(20):
        _seed_live(f"r-20261001T0000000000{index:02d}-live-{index}", placement=None)

    report = summary_module.fleet_bound_report(
        CLIVE, occupancy=20, reservation_occupancy=0
    )
    bound = next(row for row in report["bounds"] if row["name"] == "partition-cores")
    assert bound["value"] == "unknown"
    assert bound["admits_one_more"] is True

    _refuse_over_concurrency_ceiling("clive", CLIVE)
