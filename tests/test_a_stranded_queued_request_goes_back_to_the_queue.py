"""A stranded launch of a queued request goes back to the queue, not away.

An admission pass records a handover holding the queued request before it
launches, and clears it once the launch settles. A pass cut off mid-launch, as
a re-exec of the admit service is, leaves the handover and a pointer stuck in a
pre-spawn phase. Releasing that as a launch removed the pointer and the whole
run directory, and with them a request nothing would dispatch again: on 14
November queued requests vanished this way with no record a reader could find.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import dispatch_refusals, queue_admission, recovery_liveness, runs
from reckon.crew.node import CrewError

RUN_ID = "r-20261114T175044608704-queued-request"
PROJECT = "sample"


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@pytest.fixture()
def crew_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    return tmp_path / "crew"


def _stranded(*, with_handover: bool) -> tuple[dict, float]:
    launched = time.time() - recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS - 60
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.md").write_text("the request's brief\n", encoding="utf-8")
    (directory / "steering.md").write_text("steering\n", encoding="utf-8")
    runs._write_json(
        directory / "claim.json",
        {
            "run_id": RUN_ID,
            "claim_registered_at": _at(launched),
            "holder_pid": os.getpid(),
            "holder_host": socket.gethostname(),
            "launched_at": _at(launched),
            "step": "claim-published",
            "step_at": _at(launched),
        },
    )
    request = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": "s-1",
        "phase": "queued",
        "backend": "clive",
        "queued_at": _at(launched - 600),
        "created_at": _at(launched - 600),
        "node": {
            "id": "queued-node",
            "plan": "plan-a",
            "section": "s1",
            "role": "implement",
        },
    }
    if with_handover:
        runs.write_handover(request, admitted_by={"pid": os.getpid()})
    pointer = dict(request, phase="starting", launch="cli", created_at=_at(launched))
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    return pointer, time.time()


def test_a_stranded_request_from_the_queue_is_queued_again(crew_home):
    pointer, now = _stranded(with_handover=True)
    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == [
        RUN_ID
    ]
    restored = runs.read_pointer(RUN_ID)
    assert restored["phase"] == "queued"
    assert restored["node"]["id"] == "queued-node"
    assert (runs.run_dir(RUN_ID) / "brief.md").is_file()
    assert not (runs.run_dir(RUN_ID) / "claim.json").exists()
    assert runs.find_handover(RUN_ID) is None


def test_a_stranded_launch_with_no_queued_request_is_released(crew_home):
    pointer, now = _stranded(with_handover=False)
    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == [
        RUN_ID
    ]
    assert not runs.pointer_path(RUN_ID).exists()
    assert not runs.run_dir(RUN_ID).exists()


def test_the_release_fences_the_abandoned_attempt_for_an_hour(crew_home):
    pointer, now = _stranded(with_handover=True)
    queue_admission._release_stranded_launches([pointer], now_seconds=now)
    with pytest.raises(CrewError, match="released as a stranded launch"):
        dispatch_refusals.refuse_a_released_launch(RUN_ID)
    path = dispatch_refusals.launch_refusal_path(RUN_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    lapsed = datetime.now(UTC) - timedelta(
        seconds=dispatch_refusals.RELEASED_LAUNCH_FENCE_SECONDS + 60
    )
    record["recorded_at"] = lapsed.isoformat()
    runs._write_json(path, record)
    dispatch_refusals.refuse_a_released_launch(RUN_ID)
