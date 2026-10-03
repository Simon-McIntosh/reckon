"""The reservation states the reach of its roster cap where it is armed.

The published record is one host-global allocation and its roster cap is summed
over every project's placed runs, so a project that arms a reservation reaches
every other project on the host while its own flight configuration shows only
its own fields. These cases pin the remedy: the hold and the adoption report
the reach with the projects the cap counts at that moment, and the published
record carries the same reach for every later reader, so a session at the point
of the decision can see the blast radius of arming it.

The scheduler is a recording stub on the PATH and the crew state is a temporary
RECKON_HOME, so no allocation is obtained and no ambient state is read or
written. Liveness is a stub: the property under test is what the hold states
about its reach, not what the kernel answers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon.crew import placement, runs

# The job the stubbed salloc grants a freshly minted reservation, and the job
# an adopted fleet allocation is recorded under. Distinct, so a case shows
# which path published the record.
MINTED_ID = "1274099"
ADOPTED_ID = "55600002"
ADOPTED_ROW = (
    f"{ADOPTED_ID}|reckon-fleet|RUNNING|00:10:00|clu-2018||reckon-fleet|UNLIMITED"
)
ADOPTED_SHAPE = "rigel|40|128G"

PLACED = {"scheduler": "srun", "options": ["--partition=all"]}

# The reach every surface must state, and the distinction it must carry: the
# cap is summed over the whole host, not over the holding project alone.
HOST_WIDE = "counts every project's placed live runs on this host"
NOT_ONLY_HOLDER = "not only the holding project's"

# Every stub appends its own argv to this log, and squeue answers the four
# query shapes the placement code asks: the job listing, the shape probe, the
# state probe and the pending-reason probe.
_RECORDING_SCHEDULER = (
    """#!/bin/sh
name=${0##*/}
printf '%s\\t%s\\n' "$name" "$*" >> "$FAKE_SCHEDULER_LOG"
case "$name" in
  salloc)
    printf 'salloc: Granted job allocation %s\\n' "${FAKE_JOB:-__MINTED__}"
    ;;
  squeue)
    args="$*"
    case "$args" in
      *%i*) printf '%s\\n' "__ROW__" ;;
      *%P*) printf '%s\\n' "__SHAPE__" ;;
      *%T*) printf '%s\\n' "RUNNING" ;;
      *%R*) printf '%s\\n' "None" ;;
      *) : ;;
    esac
    ;;
esac
exit 0
""".replace("__MINTED__", MINTED_ID)
    .replace("__ROW__", ADOPTED_ROW)
    .replace("__SHAPE__", ADOPTED_SHAPE)
)


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
    for name in ("salloc", "sbatch", "squeue", "srun"):
        executable = directory / name
        executable.write_text(_RECORDING_SCHEDULER, encoding="utf-8")
        executable.chmod(0o755)
    return directory


def _scheduler_path(bin_dir: Path) -> str:
    """The recording stubs first on the system PATH, so they shadow any client."""
    return os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])


def _live_pointer(home: Path, run_id: str, project: str, *, placed: bool) -> None:
    """A live pointer for one project, placed inside the reservation or not."""
    pointer = {
        "run_id": run_id,
        "project": project,
        "launcher_host": os.uname().nodename,
        "pid": 4242,
        "phase": "working",
    }
    if placed:
        pointer["placement"] = dict(PLACED)
    path = runs.pointer_path(run_id)
    assert path.is_relative_to(home), "the pointer must land in the temp home"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer), encoding="utf-8")


def _publish_fleet_record(tmp_path: Path) -> None:
    """Write the fleet record naming the supervisor-running allocation."""
    directory = tmp_path / "fleet-state"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "record.json").write_text(
        json.dumps({"job_id": ADOPTED_ID, "node": "clu-2018"}), encoding="utf-8"
    )


def test_the_hold_states_the_host_wide_reach_and_the_projects_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A recorded hold names the host-wide reach and the projects it reaches.

    Two projects hold placed live runs and a third holds only an unplaced one,
    so the statement must name the two the cap counts and must not name the one
    that runs outside the reservation. The number of the cap is not enough on
    its own: the session arming it is being told whose work it throttles.
    """
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    monkeypatch.setattr(runs, "process_alive", lambda pid: True)
    _live_pointer(home, "r-alpha-1", "alpha", placed=True)
    _live_pointer(home, "r-beta-1", "beta", placed=True)
    _live_pointer(home, "r-gamma-1", "gamma", placed=False)

    result = placement.ensure_reservation(project="alpha", session="s-1")

    assert result["reason"] == "held"
    assert result["job_id"] == MINTED_ID
    detail = result["detail"]
    assert HOST_WIDE in detail
    assert NOT_ONLY_HOLDER in detail
    assert "alpha" in detail and "beta" in detail

    record = placement.read_reservation()
    assert record is not None
    reach = record["roster_reach"]
    assert reach["scope"] == "host"
    assert HOST_WIDE in reach["statement"]
    assert reach["projects"] == ["alpha", "beta"], (
        "the cap counts placed runs, so the unplaced project is not named"
    )
    assert reach["statement"] in detail, (
        "the published record carries the same reach the detail states"
    )


def test_the_adoption_states_the_same_host_wide_reach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An adopted fleet allocation publishes the reach exactly as a hold does.

    Adoption obtains nothing and starts nothing, but it is still the moment a
    reservation is armed for this host, so the record it publishes and the
    detail it returns must state the reach the cap has over the projects whose
    placed runs are already counted against it.
    """
    home = _isolate(monkeypatch, tmp_path)
    bin_dir = _recording_scheduler(tmp_path / "bin")
    monkeypatch.setenv("PATH", _scheduler_path(bin_dir))
    monkeypatch.setattr(runs, "process_alive", lambda pid: True)
    _publish_fleet_record(tmp_path)
    _live_pointer(home, "r-alpha-1", "alpha", placed=True)
    _live_pointer(home, "r-beta-1", "beta", placed=True)

    result = placement.ensure_reservation(project="gamma", session="s-2")

    assert result["reason"] == "adopted"
    assert result["job_id"] == ADOPTED_ID
    assert HOST_WIDE in result["detail"]
    assert "alpha" in result["detail"] and "beta" in result["detail"]

    record = placement.read_reservation()
    assert record is not None
    reach = record["roster_reach"]
    assert reach["scope"] == "host"
    assert reach["projects"] == ["alpha", "beta"]
    assert reach["statement"] in result["detail"]
