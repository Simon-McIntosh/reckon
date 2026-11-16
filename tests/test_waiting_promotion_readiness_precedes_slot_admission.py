"""Parked promotion readiness is independent of promotion-slot capacity."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion_requests as requests
from tests.test_review_promotion_waits_for_its_record import (
    PROJECT,
    _store_record,
    _write_exit_record,
    _write_pointer,
)
from tests.test_review_promotion_waits_for_its_record import (
    repository as review_repository,
)

repository = review_repository
NOW = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)


def _park(repository, tmp_path, number, *, session="session", complete=True):
    run_id = f"r-parked-review-{number:03}"
    reviewed = f"r-reviewed-delivery-{number:03}"
    _write_pointer(repository, tmp_path, run_id, reviewed, pid=None)
    _store_record(run_id, reviewed)
    _write_exit_record(run_id)
    pointer = requests.runs.read_pointer(run_id)
    pointer["session"] = session
    requests.runs._write_json(requests.runs.pointer_path(run_id), pointer)
    manifest = Path(pointer["manifest_path"])
    manifest.write_text("status: in-progress\n")
    request = requests.request_promotion(run_id, kind="review-self")
    waiting, reason = requests._review_self_waits(request)
    assert waiting and "manifest does not read complete" in reason
    request.update(
        phase=requests.WAITING,
        waiting_reason=reason,
        requested_at=(NOW + timedelta(seconds=number)).isoformat(),
        checked_at=NOW.isoformat(),
    )
    requests.runs._write_json(requests._request_path(run_id), request)
    if complete:
        manifest.write_text("status: complete\n")
    return request


def _earlier_ready():
    run_id = "r-earlier-ready"
    requests.runs._write_json(
        requests.runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": "session",
            "phase": "complete",
        },
    )
    request = requests.request_promotion(run_id)
    request["requested_at"] = NOW.isoformat()
    requests.runs._write_json(requests._request_path(run_id), request)
    return request


def test_both_repaired_reviews_are_rechecked_with_both_slots_held(
    repository, tmp_path, monkeypatch
):
    monkeypatch.setattr(requests, "_now", (NOW + timedelta(minutes=5)).isoformat)
    earlier = _earlier_ready()
    reviews = [_park(repository, tmp_path, number) for number in (1, 2)]
    unfinished = _park(repository, tmp_path, 3, complete=False)
    started = []

    def execute(request):
        started.append(request["run_id"])
        return (
            {"ok": True}
            if request["kind"] == "complete"
            else requests._execute(request)
        )

    service = requests.PromotionService(width=2, execute=execute)
    held = [service._claim(f"promotion-slot-{number}") for number in range(2)]
    assert all(held)
    try:
        assert requests._pending_requests()[0]["run_id"] == earlier["run_id"]
        assert service.tick() == []
        refreshed = [requests.read_promotion(row["run_id"]) for row in reviews]
        reconsidered = sum(row["checked_at"] != NOW.isoformat() for row in refreshed)
        print(
            f"full slots: reconsidered {reconsidered}/2 repaired reviews; launched {len(started)}"
        )
        assert reconsidered == 2, refreshed
        assert all(row["phase"] == requests.REQUESTED for row in refreshed)
        assert all(row["waiting_reason"] == "" for row in refreshed)
        assert all(
            row["request_id"] == original["request_id"]
            for row, original in zip(refreshed, reviews, strict=True)
        )
        assert not started
        still_waiting = requests.read_promotion(unfinished["run_id"])
        assert still_waiting["phase"] == requests.WAITING
        assert still_waiting["waiting_reason"] == unfinished["waiting_reason"]
        assert still_waiting["checked_at"] != unfinished["checked_at"]

        service._release(held.pop(0))
        expected = [earlier["run_id"], *(row["run_id"] for row in reviews)]
        for run_id in expected:
            assert service.tick() == [run_id]
            service.settle(timeout=30)
            receipt = requests.read_promotion(run_id)
            assert receipt["phase"] == requests.PROMOTED, receipt
        assert started == expected
        assert {row["run_id"] for row in ledger.runs(PROJECT, root=repository)} == {
            row["run_id"] for row in reviews
        }
        assert service.tick() == []
        assert (
            requests.read_promotion(unfinished["run_id"])["phase"] == requests.WAITING
        )
    finally:
        service.settle(timeout=30)
        for lease in held:
            service._release(lease)


def test_newly_ready_reviews_take_fair_session_turns(repository, tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "_now", (NOW + timedelta(minutes=5)).isoformat)
    first = _park(repository, tmp_path, 1)
    same_session = _park(repository, tmp_path, 2)
    other_session = _park(repository, tmp_path, 3, session="other-session")
    release = threading.Event()

    def execute(request):
        assert release.wait(10), "test did not release its promotion slots"
        return {"ok": True}

    service = requests.PromotionService(width=2, execute=execute)
    try:
        assert service.tick() == [first["run_id"], other_session["run_id"]]
    finally:
        release.set()
        service.settle(timeout=10)
    assert service.tick() == [same_session["run_id"]]
    service.settle(timeout=10)
    for row in (first, other_session, same_session):
        assert requests.read_promotion(row["run_id"])["phase"] == requests.PROMOTED


@pytest.mark.parametrize("complete", [False, True])
def test_one_wake_rechecks_a_hundred_parked_requests_under_one_second(
    repository, tmp_path, monkeypatch, capsys, complete
):
    monkeypatch.setattr(requests, "_now", (NOW + timedelta(minutes=5)).isoformat)
    _earlier_ready()
    parked = [
        _park(repository, tmp_path, number, complete=complete)
        for number in range(1, 101)
    ]
    service = requests.PromotionService(width=2)
    held = [service._claim(f"promotion-slot-{number}") for number in range(2)]
    assert all(held)
    try:
        began = time.perf_counter()
        assert service.tick() == []
        elapsed = time.perf_counter() - began
        refreshed = [requests.read_promotion(row["run_id"]) for row in parked]
        checked = sum(row["checked_at"] != NOW.isoformat() for row in refreshed)
        print(f"readiness: {checked}/100 checked in {elapsed:.6f} seconds")
        assert checked == 100
        assert all(row["phase"] == requests.WAITING for row in refreshed)
        assert all(
            "manifest does not read complete" in row["waiting_reason"]
            for row in refreshed
        )
        assert elapsed < 1.0
    finally:
        for lease in held:
            service._release(lease)
