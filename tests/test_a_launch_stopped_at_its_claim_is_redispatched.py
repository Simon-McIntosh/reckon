"""A launch that stopped at its claim is dated, released and redispatched.

A station launch publishes its dispatch claim before it composes anything else,
and a launch cut off at that step leaves a pointer holding the pre-spawn phase
with no attempt clock of its own. Nothing then dates it, so it never reaches
the stranded-launch bound, nothing frees its claim, and it reads ``starting``
until a coordinator discards it by hand. The claim is the clock such a launch
does hold, and it is the one the reader uses: past the bound with its holder
gone, the launch is unreached, its claim is released, and it is dispatched
again.

The release is paired with a fence. A launch merely slowed by storage reaches
its reservation after the release and must take nothing, or it would run a
second worker beside its own redispatch.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import (
    dispatch_launch,
    dispatch_refusals,
    queue_admission,
    recovery,
    recovery_liveness,
    runs,
)
from reckon.crew.node import CrewError

RUN_ID = "r-station-claim-published"
PROJECT = "sample"


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    assert recovery_liveness.process_alive(process.pid) is False
    return process.pid


def _station_launch(
    *, held_for_seconds: float, holder_pid: int | None = None
) -> tuple[dict, float]:
    """A station launch stopped at ``claim-published`` with nothing else written.

    The pointer is the one the claim publication writes: a pre-spawn phase, no
    pid, and no attempt clock, exactly the shape a launch cut off at that step
    leaves behind. The claim is the only record that dates it.
    """
    launched = time.time() - held_for_seconds
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    runs._write_json(
        directory / "claim.json",
        {
            "run_id": RUN_ID,
            "claim_registered_at": _at(launched),
            "holder_pid": os.getpid() if holder_pid is None else holder_pid,
            "holder_host": socket.gethostname(),
            "launched_at": _at(launched),
            "step": "claim-published",
            "step_at": _at(launched),
        },
    )
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "phase": "starting",
        "launch": "in-harness",
        "created_at": _at(launched),
        "node": {"id": "fixture-node", "role": "implement"},
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    return pointer, time.time()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised crew home; the fixture never touches the operator's."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    return tmp_path / "crew"


@pytest.mark.parametrize("holder", ["dead", "past-the-bound"])
def test_an_unreached_station_launch_frees_its_claim(
    crew_home: Path, holder: str
) -> None:
    bound = recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS
    if holder == "dead":
        pointer, now = _station_launch(
            held_for_seconds=bound + 60, holder_pid=_dead_pid()
        )
    else:
        pointer, now = _station_launch(held_for_seconds=bound + 60)

    assert recovery_liveness._stranded_launch(pointer, now_seconds=now) is True
    row = recovery.classify_pointer(pointer, now_seconds=now)
    assert row["classification"] == "launch-failed"
    assert "stranded launch" in row["detail"]

    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == [
        RUN_ID
    ]
    assert not runs.pointer_path(RUN_ID).exists()
    assert not runs.run_dir(RUN_ID).exists()
    assert dispatch_refusals.launch_refusal(RUN_ID)["reason"] == "launch-span-exceeded"


def test_a_station_launch_inside_the_bound_stays_starting(crew_home: Path) -> None:
    pointer, now = _station_launch(held_for_seconds=60)

    assert recovery_liveness._stranded_launch(pointer, now_seconds=now) is False
    row = recovery.classify_pointer(pointer, now_seconds=now)
    assert row["phase"] == "starting"
    assert row["classification"] != "launch-failed"
    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == []
    assert runs.pointer_path(RUN_ID).exists()


def test_a_released_claim_starts_no_worker_and_writes_no_reservation(
    crew_home: Path,
) -> None:
    pointer, now = _station_launch(
        held_for_seconds=recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS + 60
    )
    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == [
        RUN_ID
    ]

    with pytest.raises(CrewError, match="released as a stranded launch"):
        dispatch_launch.reserve_launch_attempt(
            runs.run_dir(RUN_ID), RUN_ID, kind="dispatch"
        )

    directory = runs.run_dir(RUN_ID)
    assert list(directory.glob("attempt-*.reservation")) == []
    assert not (directory / dispatch_launch.WORKER_RECORD_NAME).exists()
