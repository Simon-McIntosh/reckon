"""A producer shares its fleet read and yields at a bounded pointer boundary."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from reckon import flight, ledger
from reckon.crew import (
    model_catalogue,
    plan_review,
    recovery,
    recovery_review_acceptance,
    recovery_review_dispatch,
    recovery_watch,
    resumption,
    runs,
)
from tests.test_review_plan_command import project as project  # noqa: PLC0414


class ReloadCheckReachedError(Exception):
    """End a synthetic producer when it returns control between ticks."""


@pytest.fixture
def tick(project, monkeypatch):
    _home, repo, plan = project
    edited = time.time() - 11 * 60
    os.utime(plan, (edited, edited))
    monkeypatch.setattr(runs, "PRODUCER_TICK_SECONDS", 120.0, raising=False)
    created = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    for index in range(40):
        run_id = f"r-author-{index:02}"
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": "sample",
                "repo": str(repo),
                "session": "author",
                "phase": "working",
                "created_at": created,
                "node": {"id": f"author-{index}", "plan": "fixture"},
            },
        )
        stale = time.time() - 3600
        os.utime(runs.pointer_path(run_id), (stale, stale))
    producer = runs._WatchStreamProducer(
        path=runs.watch_stream_path("sample"), known={}, stall_window="10m"
    )
    # Obligations publish on their own thread; the synchronous tick is measured
    # here with that independent writer already holding its sweep lock.
    producer.sweep_lock.acquire()
    monkeypatch.setitem(runs._WATCH_STREAM_PRODUCERS, "sample", producer)

    @contextmanager
    def registration(*args, **kwargs):
        yield True, {}

    monkeypatch.setattr(recovery_watch, "_watch_registration", registration)
    monkeypatch.setattr(recovery_watch, "_refuse_unresolvable_watch", lambda _: None)
    monkeypatch.setattr(runs, "renew_watch_host_lease", lambda _: True)
    monkeypatch.setattr(recovery_watch, "watch_lease_renewed_at", lambda _: None)
    monkeypatch.setattr(
        recovery_watch, "update_watch_registration", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        recovery_watch.quota_weight, "backend_rate_statuses", lambda **kw: {}
    )
    monkeypatch.setattr(model_catalogue, "check_model_catalogue", lambda _: None)
    monkeypatch.setattr(recovery_watch, "_sweep_lift_rows", lambda *a: {})
    monkeypatch.setattr(recovery_watch, "_sweep_reset_rows", lambda *a: set())
    monkeypatch.setattr(recovery_watch, "_sweep_gate_rows", lambda *a: {})
    monkeypatch.setattr(ledger, "read_records", lambda *a, **kw: ([], 0))
    monkeypatch.setattr(plan_review, "list_plan_reviews", lambda *a, **kw: [])
    monkeypatch.setattr(
        plan_review, "_project_plan_paths", lambda _: ([("fixture", plan)], [])
    )
    dispatch = Mock(return_value={"run_id": "r-composed", "dispatched": True})
    monkeypatch.setattr(recovery, "dispatch_review_for_run", dispatch)

    listing = Mock(wraps=runs.list_live)
    for module in (
        runs,
        recovery_watch,
        resumption,
        recovery_review_dispatch,
        recovery_review_acceptance,
    ):
        monkeypatch.setattr(module, "list_live", listing)
    publish = Mock(wraps=runs._publish_watch_stream)
    monkeypatch.setattr(runs, "_publish_watch_stream", publish)
    resolve = Mock(wraps=flight.resolve)
    monkeypatch.setattr(flight, "resolve", resolve)
    return listing, publish, resolve, dispatch, producer


def test_one_whole_tick_shares_its_live_pointers_and_flight(tick):
    listing, publish, resolve, dispatch, _producer = tick
    stream = recovery_watch.watch_ticker("sample", poll_interval=0)
    try:
        events = [next(stream) for _ in range(40)]
    finally:
        stream.close()
    counts = {
        "list_live": listing.call_count,
        "publish": publish.call_count,
        "flight.resolve": resolve.call_count,
    }
    print(json.dumps(counts, sort_keys=True))
    assert len(events) == 40
    assert dispatch.call_count == 1, "the whole tick must reach the owed plan review"
    assert counts == {"list_live": 1, "publish": 1, "flight.resolve": 1}


def test_an_overdue_tick_reaches_the_reload_check_before_all_pointers(
    tick, monkeypatch
):
    _listing, _publish, _resolve, _dispatch, producer = tick
    visited = []
    original = recovery_watch._compute_watch_snapshot
    clock = [0.0]

    def slow_pointer(pointer, **kwargs):
        visited.append(pointer["run_id"])
        result = original(pointer, **kwargs)
        clock[0] += 11.0
        return result

    def reload_check(_interval):
        raise ReloadCheckReachedError

    monkeypatch.setattr(recovery_watch, "_compute_watch_snapshot", slow_pointer)
    monkeypatch.setattr(runs, "PRODUCER_TICK_SECONDS", 10.0, raising=False)
    monkeypatch.setattr(runs.time, "monotonic", lambda: clock[0])
    stream = recovery_watch.watch_ticker(
        "sample", poll_interval=0, sleeper=reload_check
    )
    try:
        with pytest.raises(ReloadCheckReachedError):
            list(stream)
    finally:
        stream.close()
    print(f"pointer visits before reload check: {len(visited)}")
    assert 0 < len(visited) < 40
    assert not producer.known, "an incomplete population must not publish departures"
