"""A delivered run's review is requested inside the window, and never silently not.

Two readings of one property, both on receipts the run itself leaves:

The first delivers a run while a stand-in station tick is held, and drives the
request from the follower's own sweep — the path an armed follower takes — then
reads the request's instant back from the run's stage receipts. A request that
waited for the tick would land after the window, so the case measures the
interval rather than the fact.

The second withholds the request's precondition — no armed follower serves the
run's session, so no sweep reaches it — and reads the obligation at ten
minutes, with the reason the run's own record gives. The boundary is asserted on
both sides: at 599 s the run is inside the window and owes nothing, and at 601 s
it owes an obligation naming the session nothing is serving.

Every instant is read from a temporary crew home. The clock is controlled by
the receipts' own writer, so the two cases agree on what "ten minutes" means.
"""

# ruff: noqa: F811 - the imported fixture is also a test parameter.

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import (
    recovery_review_dispatch,
    resumption,
    runs,
    stage_receipts,
    station,
    station_obligations,
    station_store,
)
from tests.test_scoring_dispatches_its_review import (
    CONFIG,
    _release_armed_producer,
    _scoring_pointer,
    isolated_project,  # noqa: F401 - fixture
)

PROJECT = "sample"
SESSION = "session-orchestrating"
WINDOW_SECONDS = 120


@pytest.fixture()
def controlled_clock(monkeypatch: pytest.MonkeyPatch):
    """Hold the receipts' clock still, so an interval is the one a case sets."""
    reading = {"now": datetime(2026, 1, 1, tzinfo=UTC)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return reading["now"]

    monkeypatch.setattr(stage_receipts, "datetime", Clock)
    return reading


def _delivered(run_id: str) -> str:
    """Record the delivery the way the watcher records it; return its instant."""
    stage_receipts.note_delivery({"run_id": run_id, "to_state": "completed_unpromoted"})
    instants = recovery_review_dispatch.review_stage_instants(run_id)
    assert instants["delivered_at"], instants
    return str(instants["delivered_at"])


def _held_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Thread, threading.Event, threading.Event]:
    """Run a station tick and hold it in its refresh stage.

    The tick is the reminder that the request must not depend on this thread:
    the arm that decides a sweep is due beats on its own cadence, and nothing
    the tick spends belongs to the interval being measured.
    """
    runtime = tmp_path / "station"
    runtime.mkdir()
    store = station_store.StationStore(runtime)
    active = station.Station(
        store,
        home=runs.crew_home(),
        projects=lambda: {PROJECT: runs.crew_home()},
        sweeps=station.SweepBeat(sweep=resumption.sweep, start=lambda work: work()),
    )
    entered = threading.Event()
    gate = threading.Event()

    def held_publish(*args, **kwargs):
        entered.set()
        gate.wait(timeout=60)

    monkeypatch.setattr(station_obligations, "publish", held_publish)

    def run_tick() -> None:
        active.tick()

    ticker = threading.Thread(target=run_tick, name="held-tick", daemon=True)
    ticker.start()
    return ticker, entered, gate


def test_a_delivered_run_is_requested_inside_the_window_while_a_tick_is_held(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request lands inside the window, and its instant is its own receipt."""
    config_home, repo = isolated_project
    run_id = "r-delivered-while-a-tick-is-held"
    _scoring_pointer(config_home, repo, run_id)
    ticker, entered, gate = _held_tick(tmp_path, monkeypatch)
    try:
        assert entered.wait(timeout=30), "the stand-in tick never entered its stage"
        delivered_at = _delivered(run_id)
        # The request the follower's own sweep makes, on its own thread: the
        # reflex's pass is what asks for the review, and the tick above is what
        # must not be holding it.
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            report = resumption.sweep(
                PROJECT, config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
        assert not gate.is_set(), "the tick finished, so it was not held across the request"
        # The request is named by the pass that made it, so a receipt seen below
        # is one this sweep wrote rather than one left by another path.
        assert any(
            str(row.get("run_id") or "") == run_id and row.get("dispatched")
            for row in report["reviews"].get("reports") or ()
        ), (
            "the sweep requested no review for the delivered run, so the "
            f"request waited for something other than its own pass: {report['reviews']}"
        )
        recorded = runs.read_pointer(run_id).get("review_dispatch") or {}
        assert str(recorded.get("status") or "") == "dispatched", recorded
        assert runs.pointer_path(str(recorded.get("run_id") or "")).exists()
        instants = recovery_review_dispatch.review_stage_instants(run_id)
        assert instants["requested_at"], (
            "the reflex reached the run without recording the request's instant"
        )
        assert instants["delivered_at"] == delivered_at
        census = recovery_review_dispatch.review_request_census(
            [run_id], pointers=[runs.read_pointer(run_id)]
        )
        row = census["rows"][0]
        assert row["request_lag_seconds"] is not None
        assert row["request_lag_seconds"] <= WINDOW_SECONDS, row
        # The launch is a separate interval: a slow launch is not a late request.
        assert "launch_lag_seconds" in row
        assert census["request_lag_seconds"]["p90"] == row["request_lag_seconds"]
        assert census["counts"]["requested"] == 1
        assert census["obligations"] == []
    finally:
        gate.set()
        ticker.join(timeout=30)
        _release_armed_producer()


def test_a_run_with_no_request_carries_an_obligation_at_ten_minutes(
    isolated_project: tuple[Path, Path],
    controlled_clock: dict,
) -> None:
    """Withheld precondition: past ten minutes the obligation names the reason."""
    config_home, repo = isolated_project
    run_id = "r-never-requested"
    record = _scoring_pointer(config_home, repo, run_id)
    # A session with no registration at all is the precondition: the reflex
    # reaches a run only from inside the follower of the session that owns it,
    # so nothing sweeps this one and nothing refuses it either.
    record["session"] = "session-nobody-serves"
    runs._write_json(runs.pointer_path(run_id), record)
    delivered_at = _delivered(run_id)
    moment = datetime.fromisoformat(delivered_at)

    inside = recovery_review_dispatch.review_request_census(
        [run_id],
        now=moment + timedelta(seconds=599),
        pointers=[record],
    )
    assert inside["obligations"] == []
    assert inside["counts"]["unrequested"] == 1

    overdue = recovery_review_dispatch.review_request_census(
        [run_id],
        now=moment + timedelta(seconds=601),
        pointers=[record],
    )
    assert len(overdue["obligations"]) == 1, overdue
    obligation = overdue["obligations"][0]
    assert obligation["run_id"] == run_id
    assert obligation["age_seconds"] >= 600
    assert "session-nobody-serves" in obligation["reason"], obligation
    assert overdue["counts"]["requested"] == 0


def test_a_review_run_owes_no_review_of_itself(
    isolated_project: tuple[Path, Path], controlled_clock: dict
) -> None:
    """A reviewer is not a delivered run that owes a request."""
    config_home, repo = isolated_project
    review_id = "r-20260101T000000000000-review-of-r-source"
    _scoring_pointer(config_home, repo, review_id)
    delivered_at = _delivered(review_id)
    moment = datetime.fromisoformat(delivered_at)

    census = recovery_review_dispatch.review_request_census(
        [review_id],
        now=moment + timedelta(seconds=601),
        pointers=[runs.read_pointer(review_id)],
    )
    assert census["rows"] == []
    assert census["obligations"] == []
    assert census["counts"]["delivered"] == 0


def test_a_recorded_refusal_is_the_obligations_reason(
    isolated_project: tuple[Path, Path],
) -> None:
    """A refused dispatch is named as the reason, not as no dispatch at all."""
    config_home, repo = isolated_project
    run_id = "r-refused-then-quiet"
    record = _scoring_pointer(config_home, repo, run_id)
    record["review_dispatch"] = {
        "status": "refused",
        "reason": "the lane has no room for a review this minute",
    }
    runs._write_json(runs.pointer_path(run_id), record)
    delivered_at = _delivered(run_id)
    moment = datetime.fromisoformat(delivered_at)

    census = recovery_review_dispatch.review_request_census(
        [run_id],
        now=moment + timedelta(seconds=601),
        pointers=[record],
    )
    assert len(census["obligations"]) == 1, census
    assert "the lane has no room" in census["obligations"][0]["reason"]
