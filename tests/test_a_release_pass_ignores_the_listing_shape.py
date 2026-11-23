"""A release pass retires a promoted pointer whatever shape it was listed in.

A live pointer is listed by reading the live directory: the record as decoded,
with its node body stored apart in the run's own file and the station's own pid
left in place. A by-id read re-hydrates the node body, so a pass that hands the
listing to a compare-under-lock retirement compared a slim record against a
whole one, the two differed in shape alone, and a promoted pointer whose worker
was dead was held however quiet the run was.

Each case here builds a pointer the way production writes it — a promoted run
with its ledger row, a workspace it no longer has, and no pending landing
journal — and leaves its node body in the run's file so the listing is slim.
The promotion rows are the same rows a promotion leaves behind.

The census instrument at the end prepares the population section 6 measured:
promoted pointers with dead workers, half listed slim and half carrying their
whole body, so a comparison that still depends on the record's shape shows up
as a column that does not accept them all.

The negative control runs the first case against the base revision, where the
pointer stays held: that retirement is the change this case exists to show.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import pending_landing, runs
from tests.helpers.git import git
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout

FLEET_NODE = "98dci4-clu-3026.example.invalid"
PREVIOUS_NODE = "98dci4-clu-2001.example.invalid"

#: The node-definition fields a pointer stores apart from itself. A pointer
#: carrying them is written slim, which is the listing shape under test.
BODY_FIELDS = {
    "brief": "# the node\nwork the run's worker was given\n",
    "done_when": "the case passes",
    "peer_scopes": {"peers": {}},
    "admission_findings": ["a finding"],
}


def _dead_pid() -> int:
    """A pid this host has already reaped, so its liveness reads dead."""
    process = subprocess.Popen(["/bin/true"])
    process.wait()
    assert not Path(f"/proc/{process.pid}").exists(), (
        f"pid {process.pid} was reaped but /proc still names a process"
    )
    return process.pid


def _live_pointers(home: Path) -> set[str]:
    """The live pointer file names under a crew home."""
    return {path.name for path in home.glob("live/*.json")}


@pytest.fixture()
def crew(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A temporary crew home, run store and repository, with the fleet's left alone."""
    real_home = runs.crew_home()
    before = _live_pointers(real_home)
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "run_store.db"))
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(tmp_path / "fleet-record.json"))
    (tmp_path / "fleet-record.json").write_text(json.dumps({"node": FLEET_NODE}) + "\n")
    _origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repository / "docs")}))
    (repository / "docs" / "state" / PROJECT / "runs").mkdir(
        parents=True, exist_ok=True
    )
    yield {
        "home": runs.crew_home(),
        "repository": repository,
        "head": git(repository, "rev-parse", "HEAD"),
    }
    assert _live_pointers(real_home) == before, "the case wrote into the fleet's home"


def _pointer(
    run_id: str,
    *,
    launcher_host: str,
    pid: object = None,
    worktree: str | None = None,
) -> dict:
    """One promoted-looking pointer with its node body stored apart."""
    pointer: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "session": "owner",
        "role": "implement",
        "phase": "complete",
        "launch": "in-harness",
        "launcher_host": launcher_host,
        "node": {
            "id": run_id,
            "role": "implement",
            "time_budget": "20m",
            "write_paths": ["delivered.txt"],
            **BODY_FIELDS,
        },
    }
    if pid is not None:
        pointer["pid"] = pid
    if worktree is not None:
        pointer["worktree"] = worktree
    return pointer


def _write_slim(pointer: dict) -> dict:
    """Write a pointer the way production does: the node body moved to its file."""
    runs._write_json(runs.pointer_path(str(pointer["run_id"])), pointer)
    return pointer


def _write_whole(pointer: dict) -> dict:
    """Write a pointer that still carries its node body inline, as older ones do."""
    path = runs.pointer_path(str(pointer["run_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer, indent=2, sort_keys=True), encoding="utf-8")
    return pointer


def _promote(pointer: dict, repository: Path, head: str) -> None:
    """Write the ledger row a promotion leaves for the same run."""
    ledger.write_run_file(
        PROJECT,
        {**pointer, "commits": [head], "promoted_revision": head, "gate": "passed"},
        repository,
    )


def _listed(run_id: str) -> dict:
    """One run's record as the live listing answers it."""
    for pointer in runs.list_live(project=PROJECT):
        if pointer.get("run_id") == run_id:
            return pointer
    raise AssertionError(f"{run_id} is not listed")


def test_the_previous_fleet_node_pass_retires_a_slim_listing(crew: dict) -> None:
    """A quiet promoted run from another host loses the pointer that held its claim."""
    pointer = _write_slim(
        _pointer("r-previous-node-slim", launcher_host=PREVIOUS_NODE, pid=_dead_pid())
    )
    _promote(pointer, crew["repository"], crew["head"])

    listed = _listed(pointer["run_id"])
    read = runs.read_pointer(pointer["run_id"])
    # The premise of the case: the two readings of one unchanged pointer differ
    # in shape, one carrying a body file's path and the other its values.
    assert "node_body" in listed["node"], listed["node"]
    assert "brief" not in listed["node"], listed["node"]
    assert read["node"]["brief"] == BODY_FIELDS["brief"], read["node"]

    rows = pending_landing.release_pointers_of_a_previous_fleet_node(
        fleet_node=FLEET_NODE
    )

    by_run = {row["run_id"]: row for row in rows}
    assert not runs.pointer_path(pointer["run_id"]).exists(), rows
    assert by_run[pointer["run_id"]]["status"] == (
        pending_landing.PREVIOUS_FLEET_NODE_RETIRED
    ), by_run


def test_the_ownerless_promotion_pass_retires_a_slim_listing(crew: dict) -> None:
    """A promoted run with no workspace and a dead worker loses its pointer."""
    pointer = _write_slim(
        _pointer("r-ownerless-slim", launcher_host=FLEET_NODE, pid=_dead_pid())
    )
    _promote(pointer, crew["repository"], crew["head"])

    listed = _listed(pointer["run_id"])
    assert "node_body" in listed["node"], listed["node"]
    assert pending_landing.retire_promoted_pointer(listed) == (
        pending_landing.POINTER_RETIRED
    )
    assert not runs.pointer_path(pointer["run_id"]).exists()


def test_a_pointer_rewritten_between_listing_and_retirement_is_kept(
    crew: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that went stale before the lock, while the run's pointer moved on."""
    pointer = _write_slim(
        _pointer("r-rewritten", launcher_host=PREVIOUS_NODE, pid=_dead_pid())
    )
    _promote(pointer, crew["repository"], crew["head"])
    stale = [_listed(pointer["run_id"])]

    # Another writer moves the attempt on between the listing and the lock.
    moved = runs.read_pointer(pointer["run_id"])
    moved["attempt"] = 2
    moved["phase"] = "working"
    runs._write_json(runs.pointer_path(pointer["run_id"]), moved)

    monkeypatch.setattr(runs, "list_live", lambda *args, **kwargs: stale)
    rows = pending_landing.release_pointers_of_a_previous_fleet_node(
        fleet_node=FLEET_NODE
    )

    assert runs.pointer_path(pointer["run_id"]).exists(), rows
    assert pending_landing.retire_promoted_pointer(stale[0]) == (
        pending_landing.POINTER_HELD
    )
    assert runs.pointer_path(pointer["run_id"]).exists()


def test_a_pointer_with_a_live_worker_is_kept(crew: dict) -> None:
    """A recorded process this host can still see alive outranks the promotion row."""
    pointer = _write_slim(
        _pointer("r-live-worker", launcher_host=FLEET_NODE, pid=os.getpid())
    )
    _promote(pointer, crew["repository"], crew["head"])

    listed = _listed(pointer["run_id"])
    # The comparison accepts the listing, so the pointer is kept by the live
    # process rather than by a shape the comparison still refuses.
    assert pending_landing._pointer_ownership(listed) == (
        pending_landing._pointer_ownership(runs.read_pointer(pointer["run_id"]))
    )
    assert pending_landing.retire_promoted_pointer(listed) == (
        pending_landing.POINTER_HELD
    )
    rows = pending_landing.release_pointers_of_a_previous_fleet_node(
        fleet_node=FLEET_NODE
    )
    assert [row for row in rows if row["run_id"] == pointer["run_id"]] == []
    assert runs.pointer_path(pointer["run_id"]).exists()


def test_the_census_instrument_retires_every_listing_shape(crew: dict) -> None:
    """Eleven slim listings and eleven whole ones, every one accepted and retired."""
    slims = []
    wholes = []
    for index in range(22):
        run_id = f"r-census-{index:02d}"
        pointer = _pointer(run_id, launcher_host=FLEET_NODE, pid=_dead_pid())
        if index % 2:
            slims.append(_write_slim(pointer))
        else:
            wholes.append(_write_whole(pointer))
        _promote(pointer, crew["repository"], crew["head"])

    shapes = {"slim": 0, "whole": 0}
    for pointer in slims + wholes:
        run_id = str(pointer["run_id"])
        listed = _listed(run_id)
        read = runs.read_pointer(run_id)
        if "node_body" in listed["node"]:
            shapes["slim"] += 1
        else:
            shapes["whole"] += 1
        assert pending_landing._pointer_ownership(listed) == (
            pending_landing._pointer_ownership(read)
        ), run_id
    assert shapes == {"slim": 11, "whole": 11}, shapes

    rows = pending_landing.retire_promoted_pointers(release_workspaces=False)

    retired = {row["run_id"] for row in rows}
    for pointer in slims + wholes:
        run_id = str(pointer["run_id"])
        assert run_id in retired, rows
        assert not runs.pointer_path(run_id).exists(), run_id
