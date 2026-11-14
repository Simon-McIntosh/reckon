"""A run failure in the station's sweep reaches its session's published duties."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reckon.crew import (
    obligation_snapshot,
    resumption,
    station,
    station_obligations,
    station_store,
)
from reckon.crew import recovery_review_acceptance as acceptance
from tests.test_a_review_run_promotes_itself import PROJECT
from tests.test_a_review_run_promotes_itself import (
    repository as repository,  # noqa: PLC0414 - register shared fixture
)
from tests.test_one_run_cannot_stop_a_sweep import (
    review_runs as review_runs,  # noqa: PLC0414 - register shared fixture
)

SESSION = "session-fixture"
RUN_ID = "r-review-first"
ERROR = "ValueError: unreadable ledger row"


@pytest.mark.parametrize("follower_live", [True, False])
def test_failed_run_is_published_until_a_successful_sweep(
    review_runs, tmp_path, monkeypatch, follower_live
):
    moment = datetime.now(UTC)
    failure = True
    pass_error = False
    reports = []

    def promote(pointer, **kwargs):
        if failure and pointer["run_id"] == RUN_ID:
            raise ValueError("unreadable ledger row")

    def sweep(project, **kwargs):
        if pass_error:
            return {"reviews": {"error": "review pass could not finish"}}
        result = resumption.sweep(
            project,
            pointers=review_runs,
            promotions=resumption.PromotionWorkers(width=1, wait_seconds=60),
            **kwargs,
        )
        reports.append(result)
        return result

    monkeypatch.setattr(acceptance, "promote_review_run_itself", promote)
    monkeypatch.setattr(resumption, "external_wait", lambda *a, **k: None)
    monkeypatch.setattr(resumption, "_stream_refusal_block", lambda *a: None)
    monkeypatch.setattr(
        resumption.plan_review, "compose_owed_plan_reviews", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        station_obligations,
        "follower_rows",
        lambda project: [{"session": SESSION, "live": follower_live}],
    )
    monkeypatch.setattr(
        obligation_snapshot, "fleet_state", lambda *a, **k: SimpleNamespace(findings={})
    )
    monkeypatch.setattr(
        obligation_snapshot,
        "payload_for",
        lambda state, session: {
            "project": PROJECT,
            "session": session,
            "obligations": [],
            "acknowledged": [],
            "summary": {"count": 0, "oldest_age_seconds": 0, "unreconciled_runs": 0},
        },
    )
    root = tmp_path / f"station-{follower_live}"
    home = tmp_path / f"export-{follower_live}"
    store = station_store.StationStore(root)
    beat = station.SweepBeat(sweep=sweep, start=lambda action: action())
    station.Station(store, sweeps=beat, home=home, projects=dict)

    def publish(when):
        store.heartbeat(now=when)
        station_obligations.publish(
            store, project=PROJECT, home=home, changed=False, now=when
        )
        answer = obligation_snapshot.read_station_answer(
            PROJECT,
            SESSION,
            now=when,
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "elsewhere")},
            home=home,
        )
        return None if answer is None else answer["payload"]

    try:
        assert beat.request(PROJECT, SESSION, moment=moment, changed=True)
        assert reports[0]["reviews"]["failed"] == [{"run_id": RUN_ID, "error": ERROR}]
        payload = publish(moment)
        assert payload is not None, "the station must publish a readable answer"
        rows = [row for row in payload["obligations"] if row.get("run_id") == RUN_ID]
        assert len(rows) == 1, payload
        assert ERROR in rows[0]["detail"]
        assert payload["summary"]["count"] == 1
        assert payload["session"] == SESSION
        print(f"published failure: {RUN_ID}: {rows[0]['detail']}")

        # A failed whole pass cannot attest that any run was handled successfully.
        pass_error = True
        later = moment + timedelta(seconds=20)
        assert beat.request(PROJECT, SESSION, moment=later, changed=True)
        assert publish(later)["obligations"] == payload["obligations"]

        # Reopening the writer must retain the failure until a successful pass.
        store.close()
        store = station_store.StationStore(root)
        station.Station(store, sweeps=beat, home=home, projects=dict)
        pass_error = False
        failure = False
        later += timedelta(seconds=20)
        assert beat.request(PROJECT, SESSION, moment=later, changed=True)
        assert reports[-1]["reviews"]["failed"] == []
        cleared = publish(later)
        if cleared is not None:
            assert not [
                row for row in cleared["obligations"] if row.get("run_id") == RUN_ID
            ]
            assert cleared["summary"]["count"] == 0
        print(f"cleared failure after successful pass: {RUN_ID}")
    finally:
        store.close()
