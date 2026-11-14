"""A review that has not launched is not requested for its own promotion.

The sweep holds a review back from its own promotion only while its worker is
alive. A review still waiting in the queue, on an upstream landing, or for its
launcher has no worker yet, so it fell through to a ``review-self`` request.
The request moved the pointer from ``queued`` to ``promotion-requested``, a
phase admission never launches, and the review was stranded with no record and
no way to produce one. Measured on 14 November across three projects, and
again that night for a review its coordinator had held.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import promotion_requests as requests
from reckon.crew.runs import _write_json, pointer_path
from tests import test_review_promotion_waits_for_its_record as waits

# The fixtures and helpers of the sibling review-wait module, used by name.
repository = waits.repository
real_crew_home_is_not_a_fixture_target = waits.real_crew_home_is_not_a_fixture_target


@pytest.mark.parametrize("phase", ["queued", "awaiting-upstream", "starting", "held"])
def test_an_unlaunched_review_keeps_its_phase_and_gets_no_request(
    repository: Path, tmp_path: Path, phase: str
) -> None:
    waits._write_pointer(
        repository, tmp_path, waits.FIRST_RUN_ID, waits.FIRST_REVIEWED_RUN_ID, pid=None
    )
    pointer = requests.runs.read_pointer(waits.FIRST_RUN_ID)
    pointer["phase"] = phase
    _write_json(pointer_path(waits.FIRST_RUN_ID), pointer)

    report = waits._sweep()

    assert not waits._requested(waits.FIRST_RUN_ID), report
    assert report["promotion_requested"] == [], report
    assert [entry["run_id"] for entry in report["review_self_waiting"]] == [
        waits.FIRST_RUN_ID
    ], report
    assert requests.runs.read_pointer(waits.FIRST_RUN_ID)["phase"] == phase
