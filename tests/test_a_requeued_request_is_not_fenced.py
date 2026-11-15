"""A re-queued request's next attempt is not fenced by its predecessor.

The admission pass releases a stranded launch and writes the queued request it
came from back to the queue under the same run id. The release also fences the
launch it abandoned, so a slow original resuming at its reservation takes
nothing. A fence keyed on the run refuses the request's own next attempt too,
and the request waits out the whole fence for a launch it never made: on 14
November the pass launched a request again and the retry was held for an hour.

The fence names the attempt the release took — the claim it registered and the
holder that published it — so the original is still refused at its reservation
while the next attempt, which publishes a claim of its own before it reserves,
is admitted on the same pass.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime

import pytest

from reckon.crew import (
    dispatch_launch,
    dispatch_refusals,
    queue_admission,
    recovery_liveness,
    runs,
)
from reckon.crew.node import CrewError

RUN_ID = "r-20261115T101010101010-requeued-request"
PROJECT = "sample"


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@pytest.fixture()
def crew_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    return tmp_path / "crew"


def _stranded_request() -> tuple[dict, float]:
    """A queued request the admission pass launched and left stranded."""
    launched = time.time() - recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS - 60
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brief.md").write_text("the request's brief\n", encoding="utf-8")
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
    runs.write_handover(request, admitted_by={"pid": os.getpid()})
    pointer = dict(request, phase="starting", launch="cli", created_at=_at(launched))
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    return pointer, time.time()


def _next_attempt_publishes_its_claim(*, registered: float) -> None:
    """The claim the shared dispatch path writes before it reserves.

    A launch of a queued request publishes its claim at the run id's own
    moment, so the next attempt of the re-queued request has one on disk before
    it reaches the reservation the fence guards. The holder differs as well:
    this attempt is not the process that was abandoned.
    """
    runs._write_json(
        runs.run_dir(RUN_ID) / "claim.json",
        {
            "run_id": RUN_ID,
            "claim_registered_at": _at(registered),
            "holder_pid": os.getpid() + 1,
            "holder_host": socket.gethostname(),
            "launched_at": _at(registered),
            "step": "claim-published",
            "step_at": _at(registered),
        },
    )


def _release(pointer: dict, now: float) -> None:
    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == [
        RUN_ID
    ]


def test_the_abandoned_attempt_is_still_refused_at_its_reservation(crew_home):
    pointer, now = _stranded_request()
    _release(pointer, now)
    # The requeue takes the abandoned claim with it, and no later attempt has
    # published one, so this is the released launch reaching its reservation.
    assert runs.find_handover(RUN_ID) is None
    with pytest.raises(CrewError, match="released as a stranded launch"):
        dispatch_launch.reserve_launch_attempt(
            runs.run_dir(RUN_ID), RUN_ID, kind="dispatch"
        )
    assert list(runs.run_dir(RUN_ID).glob("attempt-*.reservation")) == []


def test_the_requeued_request_s_next_attempt_is_admitted(crew_home):
    pointer, now = _stranded_request()
    _release(pointer, now)
    _next_attempt_publishes_its_claim(registered=time.time())
    dispatch_refusals.refuse_a_released_launch(RUN_ID)
    reservation = dispatch_launch.reserve_launch_attempt(
        runs.run_dir(RUN_ID), RUN_ID, kind="dispatch"
    )
    assert reservation.kind == "dispatch"
    assert (
        runs.run_dir(RUN_ID) / f"attempt-{reservation.attempt}.reservation"
    ).is_file()


def test_a_release_with_no_requeue_still_fences_the_run(crew_home):
    """A launch the release gave back authorises no second attempt, so a fresh
    dispatch of the same id is refused as it was before."""
    pointer, now = _stranded_request()
    runs.clear_handover(pointer)
    queue_admission._release_stranded_launches([pointer], now_seconds=now)
    assert dispatch_refusals.launch_refusal(RUN_ID).get("requeued") is None
    _next_attempt_publishes_its_claim(registered=time.time() + 1)
    with pytest.raises(CrewError, match="released as a stranded launch"):
        dispatch_refusals.refuse_a_released_launch(RUN_ID)


def test_a_fence_that_names_no_attempt_holds_the_whole_run(crew_home):
    """A record written before attempts were named cannot be compared."""
    pointer, now = _stranded_request()
    _release(pointer, now)
    path = dispatch_refusals.launch_refusal_path(RUN_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop("attempt")
    runs._write_json(path, record)
    _next_attempt_publishes_its_claim(registered=time.time())
    with pytest.raises(CrewError, match="released as a stranded launch"):
        dispatch_refusals.refuse_a_released_launch(RUN_ID)
