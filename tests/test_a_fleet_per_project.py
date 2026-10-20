"""A project with a fleet of its own runs its sessions and workers on its own node.

A named fleet is one whole-node allocation with its own supervisor, state,
services and worker reservation; every project it does not name lives on the
catch-all fleet, whose state stays at the state root. These cases pin the
pieces that keep fleets apart: each fleet's state directory and scheduler
comment, the services each supervisor runs, the reservation a project's workers
place into, the roster each allocation counts, and the node-local temp
directory a fleet's sessions use.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import fleet_node, fleet_supervisor, placement

CATCH_ALL_JOB = "1291586"
AMBIX_JOB = "1291700"


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    (xdg / "fleet").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "reckon-home"))
    monkeypatch.delenv(fleet_supervisor.FLEET_NAME_ENV, raising=False)
    monkeypatch.delenv(fleet_supervisor.STATE_DIR_ENV, raising=False)
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}


def _write_config(isolated: dict[str, str], name: str, payload: object) -> None:
    path = Path(isolated["XDG_CONFIG_HOME"]) / "fleet" / name
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_named_fleet_keeps_its_state_apart_from_the_catch_all(
    isolated: dict[str, str],
) -> None:
    root = Path(isolated["HOME"]) / ".local" / "state" / "fleet"
    assert fleet_supervisor.state_directory({}) == root
    assert fleet_supervisor.state_directory({"FLEET_NAME": "fleet"}) == root
    assert fleet_supervisor.state_directory({"FLEET_NAME": "ambix"}) == (
        root / "fleets" / "ambix"
    )
    assert fleet_supervisor.state_directory(
        {"FLEET_NAME": "ambix", "FLEET_STATE_DIR": "/elsewhere"}
    ) == Path("/elsewhere")
    with pytest.raises(ValueError, match="unsafe fleet name"):
        fleet_supervisor.fleet_state_directory("../escape")


def test_fleets_json_maps_each_named_fleet_to_its_projects(
    isolated: dict[str, str],
) -> None:
    _write_config(
        isolated,
        "fleets.json",
        {
            "ambix": ["imas-ambix"],
            "fleet": ["claims-the-catch-all"],
            "bad/name": ["refused"],
            "nova": "not-a-list",
        },
    )
    assert fleet_supervisor.declared_fleets() == {"ambix": ["imas-ambix"]}
    assert fleet_supervisor.fleet_for_project("imas-ambix") == "ambix"
    assert fleet_supervisor.fleet_for_project("power_convertor") == "fleet"


def test_each_supervisor_runs_only_its_own_fleets_services(
    isolated: dict[str, str],
) -> None:
    _write_config(
        isolated,
        "services.json",
        {
            "admit": ["/bin/admit", "--lane", "local"],
            "ingest": {"argv": ["/bin/ingest"], "fleet": "ambix"},
        },
    )
    assert fleet_supervisor.declared_services(dict(isolated)) == {
        "admit": ["/bin/admit", "--lane", "local"]
    }
    assert fleet_supervisor.declared_services({**isolated, "FLEET_NAME": "ambix"}) == {
        "ingest": ["/bin/ingest"]
    }


def test_the_hold_script_names_its_fleet(isolated: dict[str, str]) -> None:
    size = fleet_node.fleet_size({})
    named = fleet_node.generate_hold_script(size, log_path="/x/%j.log", fleet="ambix")
    assert "#SBATCH --comment=reckon-fleet:ambix" in named
    assert "#SBATCH --job-name=reckon-fleet:ambix" in named
    assert "export FLEET_NAME=ambix" in named
    assert (
        f"export FLEET_STATE_DIR={fleet_supervisor.fleet_state_directory('ambix')}"
        in named
    )
    catch_all = fleet_node.generate_hold_script(size, log_path="/x/%j.log")
    assert "#SBATCH --comment=reckon-fleet\n" in catch_all
    assert "FLEET_NAME" not in catch_all


def test_each_fleets_allocation_is_found_apart_from_the_others() -> None:
    jobs = [
        {"jobid": CATCH_ALL_JOB, "comment": "reckon-fleet"},
        {"jobid": AMBIX_JOB, "comment": "reckon-fleet:ambix"},
        {"jobid": "1", "comment": "run.sh"},
    ]
    assert [job["jobid"] for job in fleet_node.find_allocations(jobs)] == [
        CATCH_ALL_JOB
    ]
    assert [
        job["jobid"] for job in fleet_node.find_allocations(jobs, fleet="ambix")
    ] == [AMBIX_JOB]
    assert [fleet_node.fleet_of_allocation(job) for job in jobs] == [
        "fleet",
        "ambix",
        None,
    ]


def test_a_project_with_its_own_reservation_places_there(
    isolated: dict[str, str],
) -> None:
    placement.publish_reservation({"job_id": CATCH_ALL_JOB})
    placement.publish_reservation({"job_id": AMBIX_JOB}, "imas-ambix", keyed=True)
    assert placement.read_reservation("imas-ambix")["job_id"] == AMBIX_JOB
    assert placement.read_reservation("power_convertor")["job_id"] == CATCH_ALL_JOB
    assert placement.read_reservation()["job_id"] == CATCH_ALL_JOB


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_an_ended_project_reservation_is_withdrawn_not_replaced(
    isolated: dict[str, str],
) -> None:
    placement.publish_reservation({"job_id": CATCH_ALL_JOB})
    placement.publish_reservation({"job_id": AMBIX_JOB}, "imas-ambix", keyed=True)
    scheduler = _Scheduler()

    result = placement.ensure_reservation(
        project="imas-ambix",
        runner=scheduler,
        alive_probe=lambda record: (
            bool(record) and record.get("job_id") == CATCH_ALL_JOB
        ),
    )

    # The ambix fleet's allocation has ended: the project falls back to the
    # shared reservation, and nothing is asked of the scheduler.
    assert result["job_id"] == CATCH_ALL_JOB
    assert not placement.project_reservation_path("imas-ambix").exists()
    assert not [call for call in scheduler.calls if "salloc" in call[0]]


def test_a_live_project_reservation_is_reported_as_held(
    isolated: dict[str, str],
) -> None:
    placement.publish_reservation({"job_id": CATCH_ALL_JOB})
    placement.publish_reservation({"job_id": AMBIX_JOB}, "imas-ambix", keyed=True)

    result = placement.ensure_reservation(
        project="imas-ambix", runner=_Scheduler(), alive_probe=bool
    )

    assert result["job_id"] == AMBIX_JOB
    assert placement.project_reservation_path("imas-ambix").exists()


def test_the_roster_counts_only_runs_placed_in_the_same_allocation() -> None:
    pointers = [
        {"run_id": "same", "placement_reservation": {"job_id": AMBIX_JOB}},
        {"run_id": "other", "placement_reservation": {"job_id": CATCH_ALL_JOB}},
        {"run_id": "unrecorded"},
    ]
    kept = placement.placed_in_reservation(pointers, {"job_id": AMBIX_JOB})
    assert [pointer["run_id"] for pointer in kept] == ["same", "unrecorded"]
    # A reservation naming no job counts every placed run, as one shared one did.
    assert len(placement.placed_in_reservation(pointers, None)) == 3


def test_a_named_fleet_reserves_its_projects_when_it_starts(
    isolated: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(isolated, "fleets.json", {"ambix": ["imas-ambix", "jt60sa"]})
    calls: list[dict] = []
    monkeypatch.setattr(
        placement, "replace_reservation", lambda **kwargs: calls.append(kwargs)
    )

    reserved = fleet_supervisor.reserve_fleet_projects(
        {**isolated, "FLEET_NAME": "ambix", "SLURM_JOB_ID": AMBIX_JOB}
    )

    assert reserved == ["imas-ambix", "jt60sa"]
    assert calls == [
        {"job_id": AMBIX_JOB, "project": "imas-ambix", "keyed": True},
        {"job_id": AMBIX_JOB, "project": "jt60sa", "keyed": True},
    ]
    calls.clear()
    assert (
        fleet_supervisor.reserve_fleet_projects(
            {**isolated, "SLURM_JOB_ID": CATCH_ALL_JOB}
        )
        == []
    )
    assert calls == []


def test_sessions_keep_their_temp_on_the_node_per_allocation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "shm"
    root.mkdir()
    stale = root / f"{os.getuid()}-fleet-11"
    stale.mkdir()
    foreign = root / "someone-else-fleet-11"
    foreign.mkdir()
    environ = {
        "FLEET_SESSION_TMP_ROOT": str(root),
        "FLEET_RUNTIME_DIR": str(tmp_path / "runtime"),
        "SLURM_JOB_ID": "22",
    }

    runtime = fleet_supervisor.prepare_runtime(environ)
    removed = fleet_supervisor.remove_stale_session_tmp(environ)

    own = root / f"{os.getuid()}-fleet-22"
    assert runtime == tmp_path / "runtime"
    assert environ["TMPDIR"] == str(own)
    assert own.is_dir() and (own.stat().st_mode & 0o777) == 0o700
    assert removed == [stale]
    assert not stale.exists() and foreign.exists()
