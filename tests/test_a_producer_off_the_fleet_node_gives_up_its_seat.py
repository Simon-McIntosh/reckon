"""A producer on a host other than the fleet record's node gives up its seat.

The fleet record names the node every worker runs on. A producer on a host that
is not that node cannot probe a worker pid issued there, so a run of its project
launched on the fleet node reads unknown to it while it still holds the seat
that would let a producer on the fleet node start. The start-up decision below
is the seat owner's own: it reads the fleet record through its existing owner,
compares the producer host to the recorded node, and -- when the project has a
live run launched on that node -- releases the seat and exits, composing the
unit-disable command so a restart policy cannot bring the same misplaced
producer back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon._store import _config_home
from reckon.crew import runs, watch_unit

PROJECT = "producer-off-the-fleet-node"
FLEET_NODE = "98dci4-clu-2068"
FOREIGN_HOST = "98dci4-srv-1006"
UNIT = f"reckon-watch-{PROJECT}.service"
RUN_ID = "r-20261023T140534758900-a-producer-off-the-fleet-node-gives-up-its-seat"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A temporary config home and a temporary fleet record path.

    Every environment-resolved directory the decision reads points at a temp
    path, so nothing under the account's real configuration home is opened.
    """
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config / "mounts.json"))
    fleet_record = tmp_path / "state" / "fleet" / "record.json"
    fleet_record.parent.mkdir(parents=True)
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(fleet_record))
    return {"config": config, "fleet_record": fleet_record}


def _write_fleet_record(path: Path, node: str) -> None:
    path.write_text(
        json.dumps(
            {
                "job_id": "1291586",
                "node": node,
                "runtime_dir": str(path.parent / "run"),
                "started_at": "2026-10-23T14:05:34Z",
            }
        ),
        encoding="utf-8",
    )


def _write_run(project: str, run_id: str, launcher_host: str) -> None:
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "launcher_host": launcher_host,
            "phase": "working",
            "pid": 4242,
            "session": "s",
        },
    )


def _write_seat(project: str, host: str) -> None:
    path = runs.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"project": project, "host": host, "pid": 2147483646, "unit": UNIT}),
        encoding="utf-8",
    )


def _decision(host: str, *, environ: dict[str, str] | None = None):
    return watch_unit.producer_start_decision(
        PROJECT,
        host=host,
        environ={} if environ is None else environ,
    )


def test_a_foreign_host_with_runs_on_the_fleet_node_releases_and_exits(
    home: dict[str, Path],
) -> None:
    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_seat(PROJECT, FOREIGN_HOST)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)

    verdict = _decision(FOREIGN_HOST, environ={watch_unit.WATCH_UNIT_ENV: UNIT})

    assert verdict["action"] == "release-and-exit"
    assert verdict["host"] == FOREIGN_HOST
    assert verdict["fleet_node"] == FLEET_NODE
    assert verdict["runs"] == [RUN_ID]
    assert verdict["unit"] == UNIT
    assert verdict["unit_disable_command"] == [
        "systemctl",
        "--user",
        "disable",
        "--now",
        UNIT,
    ]
    # The release is announced once, naming both hosts.
    assert FOREIGN_HOST in verdict["announcement"]
    assert FLEET_NODE in verdict["announcement"]


def test_the_unit_disable_command_is_composed_only_when_the_unit_is_named(
    home: dict[str, Path],
) -> None:
    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)

    verdict = _decision(FOREIGN_HOST, environ={})

    assert verdict["action"] == "release-and-exit"
    assert verdict["unit"] is None
    assert verdict["unit_disable_command"] is None


def test_the_same_host_holds_the_seat(home: dict[str, Path]) -> None:
    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)

    verdict = _decision(FLEET_NODE, environ={watch_unit.WATCH_UNIT_ENV: UNIT})

    assert verdict["action"] == "hold"
    assert verdict["unit_disable_command"] is None


def test_an_absent_fleet_record_holds_the_seat(home: dict[str, Path]) -> None:
    # No record written at the path the environment names.
    _write_run(PROJECT, RUN_ID, FLEET_NODE)

    verdict = _decision(FOREIGN_HOST, environ={watch_unit.WATCH_UNIT_ENV: UNIT})

    assert verdict["action"] == "hold"
    assert verdict["fleet_node"] == ""


def test_a_foreign_host_with_no_run_on_the_fleet_node_holds(
    home: dict[str, Path],
) -> None:
    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    # The project's only live run was launched on the producer's own host.
    _write_run(PROJECT, RUN_ID, FOREIGN_HOST)

    verdict = _decision(FOREIGN_HOST, environ={watch_unit.WATCH_UNIT_ENV: UNIT})

    assert verdict["action"] == "hold"


def test_the_real_fleet_record_and_seat_are_untouched(
    home: dict[str, Path],
) -> None:
    real_fleet = Path.home() / ".local" / "state" / "fleet" / "record.json"
    real_watch = _config_home({}) / "crew" / "watch"
    real_fleet_before = real_fleet.stat().st_mtime_ns if real_fleet.exists() else None
    real_digest_before = real_fleet.read_bytes() if real_fleet.exists() else None
    real_seat_before = sorted(p.name for p in real_watch.glob(f"{PROJECT}-*.lock"))

    _write_fleet_record(home["fleet_record"], FLEET_NODE)
    _write_seat(PROJECT, FOREIGN_HOST)
    _write_run(PROJECT, RUN_ID, FLEET_NODE)
    _decision(FOREIGN_HOST, environ={watch_unit.WATCH_UNIT_ENV: UNIT})

    # Guarded like the before-readings above: a host with no fleet record has
    # none to have left alone, and an unguarded stat fails the test on a
    # machine that never ran a fleet.
    assert (real_fleet.stat().st_mtime_ns if real_fleet.exists() else None) == (
        real_fleet_before
    )
    assert (
        real_fleet.read_bytes() if real_fleet.exists() else None
    ) == real_digest_before
    assert (
        sorted(p.name for p in real_watch.glob(f"{PROJECT}-*.lock")) == real_seat_before
    )
