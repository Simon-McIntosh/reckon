"""Station ticks carry deliveries through review dispatch and service landing."""

from __future__ import annotations

from datetime import UTC, datetime

from reckon import _plan_html, ledger
from reckon.crew import promotion_requests, resumption, runs, station, station_store
from reckon.crew import review as review_module
from tests.helpers.git import git
from tests.test_a_queued_run_is_admitted import admit_repo  # noqa: F401
from tests.test_a_reflex_owned_review_never_blocks_a_dispatch import (
    RUN as DISPATCH_RUN,
)
from tests.test_a_reflex_owned_review_never_blocks_a_dispatch import (
    _publish,
    delivered,  # noqa: F401
)
from tests.test_cleanly_reviewed_run_lands_itself import (
    PLAN,
    PROJECT,
    RUN,
    _on_primary,
    delivery,  # noqa: F401
)


def _station_tick(tmp_path, monkeypatch, *, config=None, launcher=None):
    reports = []

    def sweep(project, **kwargs):
        report = resumption.sweep(project, config=config, launcher=launcher, **kwargs)
        reports.append(report)
        return report

    # Duty publication is a separate reader; run the station's actual sweep
    # inline so the tick's own report is the observation boundary.
    monkeypatch.setattr(station.station_obligations, "publish", lambda *a, **k: None)
    beat = station.SweepBeat(sweep=sweep, start=lambda action: action())
    with station_store.StationStore(tmp_path / "station") as store:
        producer = station.Station(store, projects=dict, sweeps=beat)
        producer.tick(now=datetime.now(UTC))
    assert len(reports) == 1, reports
    assert not reports[0]["reviews"].get("error"), reports
    return reports[0]["reviews"]


def test_one_station_tick_dispatches_the_delivered_runs_review(
    delivered,  # noqa: F811 - shared fixture
    tmp_path,
    monkeypatch,
):
    pointer, config = delivered
    _publish(pointer, age=600)
    launches = []

    def launcher(plan, **kwargs):
        launches.append(plan)
        return 0

    report = _station_tick(tmp_path, monkeypatch, config=config, launcher=launcher)

    assert len(report["dispatched"]) == 1, report
    dispatch = runs.read_pointer(DISPATCH_RUN)["review_dispatch"]
    assert dispatch["status"] == "dispatched", dispatch
    assert dispatch["run_id"] == report["dispatched"][0]
    assert launches, report
    reviewer = runs.read_pointer(dispatch["run_id"])
    assert reviewer["role"] == "review"


def test_a_station_delivery_is_promoted_and_landed_by_the_service(
    delivery,  # noqa: F811 - shared fixture
    tmp_path,
    monkeypatch,
):
    assert not _on_primary(delivery)
    assert ledger.runs(PROJECT, root=delivery["repo"]) == []

    report = _station_tick(tmp_path, monkeypatch)

    assert report["promotion_requested"] == [RUN], report
    assert promotion_requests.read_promotion(RUN)["kind"] == "acceptance"
    service = promotion_requests.PromotionService(width=1)
    assert service.tick() == [RUN]
    assert service.wait_for_in_flight(timeout=90) == []
    receipt = promotion_requests.read_promotion(RUN)
    assert receipt["phase"] == "promoted", receipt
    assert receipt["result"]["merged"] is True, receipt
    assert _on_primary(delivery), receipt
    assert (delivery["repo"] / "left.txt").read_text() == "feature\n"
    assert ledger.runs(PROJECT, root=delivery["repo"])[0]["promoted_by"] == (
        "review-acceptance"
    )
    committed = _plan_html.read_state(
        git(delivery["repo"], "show", f"HEAD:docs/plans/{PLAN}.html")
    )
    assert committed["section_declarations"]["s2"] == "done"


def test_the_service_refuses_a_review_that_gains_a_blocking_finding(
    delivery,  # noqa: F811 - shared fixture
    tmp_path,
    monkeypatch,
):
    report = _station_tick(tmp_path, monkeypatch)
    assert report["promotion_requested"] == [RUN], report
    record = review_module.read_review(PROJECT, RUN)
    assert record["scores"] == dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20)
    record["findings"] = [
        {
            "file": "left.txt",
            "line": "1",
            "text": "The delivered behavior needs repair before landing.",
            "severity": "blocking",
        }
    ]
    review_module.store_review(record)

    service = promotion_requests.PromotionService(width=1)
    assert service.tick() == [RUN]
    assert service.wait_for_in_flight(timeout=90) == []

    receipt = promotion_requests.read_promotion(RUN)
    assert receipt["phase"] == "promotion-deferred", receipt
    assert not _on_primary(delivery), receipt
    assert (delivery["repo"] / "left.txt").read_text() == "base\n"
    assert ledger.runs(PROJECT, root=delivery["repo"]) == []
    assert runs.pointer_path(RUN).is_file()
