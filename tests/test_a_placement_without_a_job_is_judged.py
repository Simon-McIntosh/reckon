"""A promoted run with no job id is judged from what its launcher left behind.

The fleet's census found live pointers whose placement named no readable job
id: the scheduler could not be reached about them at all, so their liveness
read unknown and each one was kept, although every pid they recorded was gone
from the process table. Unknown is the right reading only when nothing else can
be read.

So a placed run with no job id is judged from the two readings this host does
have: the process table for the pid the launcher recorded, and the run's own
worker record for when anything of the run last touched disk. The run is dead
when both say the worker is gone, and alive while the pid answers. A run whose
facts cannot be read is reported once as an obligation naming what is missing,
rather than held without a word; a later pass over the unchanged pointer stays
silent, and a pointer whose missing fact moved is reported again.

The negative control runs the first case at the base revision, where the
pointer is held as unknown: that release is the change this case exists to
show.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import pending_landing, runs
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout

FLEET_NODE = "98dci4-clu-3026.example.invalid"
OTHER_NODE = "98dci4-clu-2001.example.invalid"

# The placement a queued dispatch records: the scheduler is named and its state
# placement spells the query, but no job id was ever written for the run.
PLACEMENT = {
    "scheduler": "slurm",
    "state_query": ["squeue", "-h", "-j", "{job}", "-o", "%T"],
}

QUIET_SECONDS = 2 * 3600.0


def _absent_pid() -> int:
    """A pid the kernel will never allocate."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _live_pointers(home: Path) -> set[str]:
    return {path.name for path in home.glob("live/*.json")}


@pytest.fixture()
def crew(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A temporary crew home and run store, with the crew's own left untouched."""
    real_home = runs.crew_home()
    before = _live_pointers(real_home)
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "run_store.db"))
    record = tmp_path / "fleet.json"
    record.write_text(json.dumps({"node": FLEET_NODE}) + "\n")
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(record))
    _origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repository / "docs")}))
    (repository / "docs" / "state" / PROJECT / "runs").mkdir(
        parents=True, exist_ok=True
    )
    yield {"repository": repository}
    assert _live_pointers(real_home) == before, "the case wrote into the fleet's home"


def _write_pointer(
    run_id: str,
    *,
    launcher_host: str | None = FLEET_NODE,
    pid: object = None,
    job_id: str = "",
    placement: dict | None = None,
    worker_record_age_seconds: float | None = None,
    project: str = PROJECT,
    session: str = "owner",
    promoted: bool = True,
    write_directly: bool = False,
) -> dict:
    """Write one live pointer, and the worker record its liveness is read from.

    ``write_directly`` writes the file without a launcher host, which is the
    shape a pointer predating that field has: a writer using the ordinary path
    stamps the host it observes, so only a direct write can build the case.
    """
    pointer = {
        "run_id": run_id,
        "project": project,
        "repo": "/nonexistent/repository",
        "session": session,
        "role": "implement",
        "phase": "complete",
        "launch": "in-harness",
    }
    if launcher_host is not None:
        pointer["launcher_host"] = launcher_host
    if pid is not None:
        pointer["pid"] = pid
    if job_id:
        pointer["job_id"] = job_id
    if placement is not None:
        pointer["placement"] = placement
    if promoted:
        pointer["promoted"] = True
    path = runs.pointer_path(run_id)
    if write_directly:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pointer), encoding="utf-8")
    else:
        runs._write_json(path, pointer)
    if worker_record_age_seconds is not None:
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        record = directory / "worker.json"
        record.write_text(json.dumps({"pid": 1, "launched_at": "2026-01-01T00:00:00Z"}))
        moment = time.time() - worker_record_age_seconds
        os.utime(record, (moment, moment))
    return pointer


def _promote(pointer: dict, repository: Path) -> None:
    ledger.write_run_file(
        PROJECT,
        {
            **pointer,
            "commits": ["0" * 40],
            "promoted_revision": "0" * 40,
            "gate": "passed",
        },
        repository,
    )


def _judge() -> dict[str, dict]:
    """One pass of the release pass that judges a placed run with no job id."""
    rows = pending_landing.release_pointers_of_a_previous_fleet_node()
    return {row["run_id"]: row for row in rows}


def test_a_jobless_run_with_no_process_and_a_quiet_record_is_released(
    crew: dict,
) -> None:
    pointer = _write_pointer(
        "r-jobless-dead",
        pid=_absent_pid(),
        placement=PLACEMENT,
        worker_record_age_seconds=QUIET_SECONDS,
    )
    _promote(pointer, crew["repository"])

    results = _judge()

    assert not runs.pointer_path(pointer["run_id"]).exists(), results
    assert results[pointer["run_id"]]["status"] == (
        pending_landing.PREVIOUS_FLEET_NODE_RETIRED
    )


def test_the_same_pointer_with_a_live_process_keeps_its_claim(crew: dict) -> None:
    pointer = _write_pointer(
        "r-jobless-alive",
        pid=os.getpid(),
        placement=PLACEMENT,
        worker_record_age_seconds=QUIET_SECONDS,
    )
    _promote(pointer, crew["repository"])

    _judge()

    assert runs.pointer_path(pointer["run_id"]).exists()


def test_a_pointer_without_a_readable_node_is_reported_once(crew: dict) -> None:
    pointer = _write_pointer(
        "r-jobless-unknown-node",
        launcher_host=None,
        pid=_absent_pid(),
        placement=PLACEMENT,
        worker_record_age_seconds=QUIET_SECONDS,
        write_directly=True,
    )
    _promote(pointer, crew["repository"])

    first = _judge()
    assert runs.pointer_path(pointer["run_id"]).exists()
    obligation = first[pointer["run_id"]]
    assert obligation["status"] == pending_landing.RELEASE_OBLIGATION_RECORDED
    assert "launcher host" in obligation["missing"]
    stored = json.loads(
        pending_landing.release_obligation_path(pointer["run_id"]).read_text()
    )
    assert stored["missing"] == obligation["missing"]

    second = _judge()
    assert pointer["run_id"] not in second, second


def test_a_changed_missing_fact_is_reported_again(crew: dict) -> None:
    pointer = _write_pointer(
        "r-jobless-changed",
        launcher_host=None,
        pid=_absent_pid(),
        placement=PLACEMENT,
        worker_record_age_seconds=QUIET_SECONDS,
        write_directly=True,
    )
    _promote(pointer, crew["repository"])
    first = _judge()[pointer["run_id"]]

    rewritten = json.loads(runs.pointer_path(pointer["run_id"]).read_text())
    rewritten.pop("pid", None)
    rewritten["launcher_host"] = FLEET_NODE
    runs.pointer_path(pointer["run_id"]).write_text(json.dumps(rewritten))

    second = _judge()[pointer["run_id"]]
    assert second["status"] == pending_landing.RELEASE_OBLIGATION_RECORDED
    assert second["missing"] != first["missing"]
    assert "pid" in second["missing"]
