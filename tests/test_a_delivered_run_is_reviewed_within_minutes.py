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


def _first_receipt(run_id: str, stage: str) -> str:
    """The first instant the run's journal records for one stage."""
    instants = [
        str(row["at"])
        for row in stage_receipts.read_receipts(run_id)
        if row.get("stage") == stage and row.get("at")
    ]
    assert instants, f"no {stage} receipt for {run_id}"
    return min(instants)


def _receipt_gap(run_id: str, first: str, second: str) -> float:
    """The interval between two stages, read from the journal."""
    return (
        datetime.fromisoformat(_first_receipt(run_id, second))
        - datetime.fromisoformat(_first_receipt(run_id, first))
    ).total_seconds()


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
        # The instant the census reads is the first receipt the run's own
        # journal holds for the delivery, read here independently of the census.
        assert instants["delivered_at"] == _first_receipt(run_id, "delivery_recorded")
        census = recovery_review_dispatch.review_request_census(
            [run_id], pointers=[runs.read_pointer(run_id)]
        )
        row = census["rows"][0]
        assert row["request_lag_seconds"] is not None
        assert row["request_lag_seconds"] <= WINDOW_SECONDS, row
        # The interval is the distance between the two receipts, not a figure
        # the census chose: recompute it from the journal.
        assert row["request_lag_seconds"] == _receipt_gap(
            run_id, "delivery_recorded", "review_requested"
        )
        # The launch is a separate interval, read from its own receipt: the
        # admission is recorded on the reviewed run the way `note_review` writes
        # it, and the interval is asserted against the journal rather than
        # against the census's own arithmetic: a stage the census stops mapping
        # leaves `launched_at` unset and this interval unknown.
        assert row["launched_at"] is None, "the fixture admitted a review on its own"
        admitted_at = (
            datetime.fromisoformat(instants["requested_at"]) + timedelta(seconds=30)
        ).isoformat()
        started_at = (
            datetime.fromisoformat(instants["requested_at"]) + timedelta(seconds=45)
        ).isoformat()
        stage_receipts.record_stage(run_id, "review_admitted", at=admitted_at)
        stage_receipts.record_stage(run_id, "review_started", at=started_at)
        read_back = recovery_review_dispatch.review_request_census(
            [run_id], pointers=[runs.read_pointer(run_id)]
        )["rows"][0]
        assert read_back["launched_at"] == admitted_at, read_back
        assert read_back["started_at"] == started_at, read_back
        assert read_back["launch_lag_seconds"] == _receipt_gap(
            run_id, "review_requested", "review_admitted"
        )
        assert read_back["launch_lag_seconds"] == 30.0, read_back
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


def test_a_recorded_dispatch_without_a_receipt_is_not_an_obligation(
    isolated_project: tuple[Path, Path], controlled_clock: dict
) -> None:
    """A missing instant is a gap in the clock, not an absence of the request."""
    config_home, repo = isolated_project
    run_id = "r-dispatched-before-receipts"
    record = _scoring_pointer(config_home, repo, run_id)
    record["review_dispatch"] = {
        "status": "dispatched",
        "run_id": "r-20260101T000000000000-review-of-r-dispatched-before-receipts",
    }
    runs._write_json(runs.pointer_path(run_id), record)
    delivered_at = _delivered(run_id)
    moment = datetime.fromisoformat(delivered_at)

    census = recovery_review_dispatch.review_request_census(
        [run_id],
        now=moment + timedelta(seconds=601),
        pointers=[record],
    )
    assert census["obligations"] == [], census
    assert census["counts"]["request_unrecorded"] == 1
    assert census["counts"]["unrequested"] == 0
    assert census["request_unrecorded"][0]["status"] == "dispatched"


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


def test_the_census_quantiles_are_the_samples_own_rule(
    isolated_project: tuple[Path, Path], controlled_clock: dict
) -> None:
    """Two lags of known size: the p50 names the near one, the p90 the far one."""
    config_home, repo = isolated_project
    records = []
    for run_id, lag in (("r-lag-near", 5), ("r-lag-far", 100)):
        record = _scoring_pointer(config_home, repo, run_id)
        stage_receipts.record_stage(
            run_id,
            "delivery_recorded",
            at="2026-01-01T00:00:00.000000Z",
        )
        stage_receipts.record_stage(
            run_id,
            "review_requested",
            at=(
                datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(seconds=lag)
            ).isoformat().replace("+00:00", "Z"),
        )
        records.append(record)

    census = recovery_review_dispatch.review_request_census(
        [str(record["run_id"]) for record in records], pointers=records
    )
    assert [row["request_lag_seconds"] for row in census["rows"]] == [5.0, 100.0]
    assert census["request_lag_seconds"] == {"p50": 5.0, "p90": 100.0, "max": 100.0}


def test_a_request_receipted_before_its_delivery_is_not_an_obligation(
    isolated_project: tuple[Path, Path],
) -> None:
    """An out-of-order pair is an interval nobody can quote, not a request never made."""
    config_home, repo = isolated_project
    run_id = "r-receipts-out-of-order"
    record = _scoring_pointer(config_home, repo, run_id)
    stage_receipts.record_stage(
        run_id, "delivery_recorded", at="2026-01-01T00:10:00.000000Z"
    )
    stage_receipts.record_stage(
        run_id, "review_requested", at="2026-01-01T00:00:00.000000Z"
    )

    census = recovery_review_dispatch.review_request_census(
        [run_id],
        now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        pointers=[record],
    )
    assert census["counts"]["unrequested"] == 0
    assert census["request_unrecorded"][0]["status"] == "out-of-order"
    assert census["obligations"] == []


def test_a_live_pointer_says_a_run_is_a_review_without_a_prefixed_id(
    isolated_project: tuple[Path, Path],
) -> None:
    """The pointer answers too: a reviewer whose id carries no prefix owes nothing."""
    config_home, repo = isolated_project
    run_id = "r-hands-off-reviewer"
    record = _scoring_pointer(config_home, repo, run_id)
    record["node"] = {**(record.get("node") or {}), "id": "review-of-r-source"}
    runs._write_json(runs.pointer_path(run_id), record)
    _delivered(run_id)

    census = recovery_review_dispatch.review_request_census(
        [run_id],
        now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        pointers=[record],
    )
    assert census["rows"] == []


def test_the_review_requests_the_priority_of_the_run_it_reviews(
    isolated_project: tuple[Path, Path], controlled_clock: dict, monkeypatch
) -> None:
    """The request lands in the queue ranked as the reviewed run was."""
    config_home, repo = isolated_project
    run_id = "r-prioritised-source"
    record = _scoring_pointer(config_home, repo, run_id)
    record["priority_override"] = 7
    runs._write_json(runs.pointer_path(run_id), record)
    _delivered(run_id)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        report = resumption.sweep(
            PROJECT, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )
    dispatched = [
        row for row in report["reviews"].get("reports") or () if row.get("dispatched")
    ]
    assert dispatched, report["reviews"]
    review_run_id = str(dispatched[0].get("run_id") or "")
    assert review_run_id, dispatched
    review = runs.read_pointer(review_run_id)
    # The review's pointer is built from the run it reviews, so the request
    # carries that run's ranking without the dispatch choosing one: the value a
    # coordinator set on the reviewed run is the value the queue orders its
    # review by.
    assert review.get("priority_override") == 7, review
    _release_armed_producer()


def test_an_unrequested_runs_obligation_reaches_the_duty_rows(
    isolated_project: tuple[Path, Path], tmp_path: Path, controlled_clock: dict
) -> None:
    """The reason the census found is the reason the pane's duty row carries.

    A report nobody reads is not a reminder, so the case follows the obligation
    out of the sweep and into the station's own failure record, which is what
    the obligations view renders. The row is read back from that record, not
    from the report the sweep returned.
    """
    config_home, repo = isolated_project
    run_id = "r-owed-and-unserved"
    record = _scoring_pointer(config_home, repo, run_id)
    record["session"] = "session-nobody-serves"
    runs._write_json(runs.pointer_path(run_id), record)
    delivered_at = _delivered(run_id)

    controlled_clock["now"] = datetime.fromisoformat(delivered_at) + timedelta(
        seconds=601
    )
    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        report = resumption.sweep(
            PROJECT, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )
    owed = [
        row
        for row in report["reviews"].get("failed") or ()
        if str(row.get("run_id") or "") == run_id
    ]
    assert owed, report["reviews"]
    assert (
        "no armed follower serves session 'session-nobody-serves'" in owed[0]["error"]
    ), owed[0]

    store = station_store.StationStore(tmp_path / "station")
    station_obligations.record_sweep_report(store, PROJECT, SESSION, report)
    rows = station_obligations._with_sweep_failures(
        {"project": PROJECT, "obligations": [], "summary": {}},
        [
            failure
            for failures in station_obligations.prune_sweep_failures(
                store, PROJECT
            ).values()
            for failure in failures
        ],
    )["obligations"]
    named = [row for row in rows if str(row.get("run_id") or "") == run_id]
    assert named, rows
    assert (
        "no armed follower serves session 'session-nobody-serves'"
        in named[0]["detail"]
    ), named[0]
    _release_armed_producer()
