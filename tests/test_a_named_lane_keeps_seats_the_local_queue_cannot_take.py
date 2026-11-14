"""A dispatch naming another lane finds a seat the local queue cannot take.

One placement reservation seats every lane's workers. The admit service
launches a queued local request the moment a seat frees, while a dispatch named
to codex, the fast escape hatch, is refused at a full roster and cannot queue.
On 14 November a codex review was refused seven times in thirty minutes at 25
of 25, every freed seat having gone to local work first. The local lane now
stops short of the cap by a reserve that only a named lane can use.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from reckon.crew import placement, runs
from reckon.crew.dispatching import preparation
from tests.test_crew_placement_reservation import _isolate, _placed_backend

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PLACED = {"placement": {"scheduler": "srun", "options": ["--partition=all"]}}
LIMIT = placement.RESERVATION_ROSTER_LIMIT
RESERVE = placement.LOCAL_LANE_RESERVE


def _occupied(count: int) -> list[dict]:
    return [{"run_id": f"r-{index}", **PLACED} for index in range(count)]


@pytest.fixture
def reservation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    placement.publish_reservation({"job_id": "1274051"})


def test_the_reserve_is_a_few_seats_below_the_cap():
    assert 0 < RESERVE < LIMIT


def test_the_local_lane_stops_short_of_the_cap(reservation):
    dispatch_module._refuse_over_reservation_roster(
        _placed_backend(), _occupied(LIMIT - RESERVE - 1), reserve=RESERVE
    )
    with pytest.raises(runs.CrewError) as refused:
        dispatch_module._refuse_over_reservation_roster(
            _placed_backend(), _occupied(LIMIT - RESERVE), reserve=RESERVE
        )
    assert "kept for a dispatch naming another lane" in str(refused.value)


def test_a_named_lane_takes_the_reserved_seats(reservation):
    for taken in range(LIMIT - RESERVE, LIMIT):
        dispatch_module._refuse_over_reservation_roster(
            _placed_backend(), _occupied(taken)
        )


def test_a_named_lane_still_stops_at_the_cap(reservation):
    with pytest.raises(runs.CrewError):
        dispatch_module._refuse_over_reservation_roster(
            _placed_backend(), _occupied(LIMIT)
        )


def test_only_the_local_lane_holds_seats_back():
    assert preparation._local_lane_reserve(True) == RESERVE
    assert preparation._local_lane_reserve(False) == 0
