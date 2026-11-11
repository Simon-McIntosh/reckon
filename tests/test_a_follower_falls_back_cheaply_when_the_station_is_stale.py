"""A stale station costs one bounded pointer reading, with no review walk."""

from __future__ import annotations

import json
import sys
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from reckon import crew_follow_commands as follow
from reckon.crew import (
    recovery,
    recovery_review_dispatch,
    runs,
    station_store,
    station_subscriber,
)

PROJECT = "fixture"
SESSION = "own-session"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _count_bindings(monkeypatch, target, counts, key, replacement=None):
    """Count every imported alias, including lazy package exports already bound."""

    def counted(*args, **kwargs):
        counts[key] += 1
        return (replacement or target)(*args, **kwargs)

    bindings = []
    for module_name, module in list(sys.modules.items()):
        if module_name == "reckon" or module_name.startswith("reckon."):
            for name, value in list(vars(module).items()):
                if value is target:
                    monkeypatch.setattr(module, name, counted)
                    bindings.append(f"{module_name}.{name}")
    assert bindings, key
    return bindings


@pytest.fixture
def fleet(tmp_path, monkeypatch, home):
    clock = Clock()
    paths = []
    for index in range(40):
        path = runs.pointer_path(f"run-{index}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": f"run-{index}",
                    "project": PROJECT,
                    "session": SESSION,
                    "phase": "working" if index == 0 else "complete",
                }
            )
        )
        paths.append(path)
    assert len(runs._list_live_records(project=PROJECT)) == 40
    monkeypatch.setattr(runs, "renew_producer_lease", lambda *args: None)
    monkeypatch.setattr(station_subscriber, "_review_grace", lambda project: 0)
    monkeypatch.setattr(
        station_subscriber, "run_owes_its_coordinator", lambda *a, **k: False
    )
    # Force the lazy export to bind before instrumenting every alias.
    assert recovery._review_in_flight is recovery_review_dispatch._review_in_flight
    counts = Counter()
    bindings = {}
    for key, target, replacement in (
        ("fallback", follow._population_has_live_work, None),
        ("populations", station_subscriber.live_work_populations, None),
        ("reviews", recovery._review_in_flight, lambda *a, **k: ""),
    ):
        bindings[key] = _count_bindings(monkeypatch, target, counts, key, replacement)
    # A known terminal population must reach the review instrument before its
    # absence on the follower path is evidence of anything.
    station_subscriber.live_work_populations(runs._list_live_records(project=PROJECT))
    assert counts["reviews"] == 40
    counts.clear()
    with station_store.StationStore(tmp_path / "station") as store:
        station_subscriber.record_live_work(
            store,
            PROJECT,
            at=datetime.now(tz=UTC)
            - timedelta(seconds=station_subscriber.STALE_HEARTBEAT_SECONDS + 60),
            project_live=True,
            unowned=False,
            sessions=[SESSION],
        )
        subscriber = station_subscriber.StationSubscriber(
            store, PROJECT, session=SESSION
        )
        assert subscriber.live_work(SESSION) is None
        yield clock, subscriber, counts, bindings, paths


def _samples(fleet, *, seconds, grace=0, change=None, observed=()):
    clock, subscriber, _counts, _bindings, paths = fleet
    stop = threading.Event()
    sampled = []

    def sleep(interval):
        sampled.append(clock.now)
        clock.now += interval
        if change is not None:
            change(clock.now, subscriber, paths)
        if clock.now >= seconds:
            stop.set()

    events = list(
        follow._follow_watch_lines(
            PROJECT,
            session=SESSION,
            observed=observed,
            poll_interval=1,
            sleeper=sleep,
            clock=clock,
            stop=stop,
            settle=True,
            settle_interval=grace,
            sweep=None,
            subscriber=subscriber,
        )
    )
    return events, sampled


def test_stale_reading_reuses_pointer_verdict_for_sixty_samples(fleet):
    events, sampled = _samples(fleet, seconds=60)
    _, _, counts, bindings, _ = fleet
    assert len(sampled) == 60
    assert counts["fallback"] <= 2 and counts["populations"] <= 2, (
        dict(counts),
        bindings,
    )
    assert counts["populations"] >= 1, "the population instrument saw no fallback"
    assert counts["reviews"] == 0, dict(counts)
    assert not events


@pytest.mark.parametrize("grace", [0, 10])
def test_terminal_pointer_is_read_within_one_interval_and_stale_grace_is_extended(
    fleet, monkeypatch, grace
):
    clock, _, counts, _, _paths = fleet
    readings = []
    original = station_subscriber.live_work_populations

    def read(*args, **kwargs):
        verdict = original(*args, **kwargs)
        readings.append((clock.now, verdict[0]))
        return verdict

    monkeypatch.setattr(station_subscriber, "live_work_populations", read)

    def finish(now, subscriber, paths):
        if now == 1:
            pointer = json.loads(paths[0].read_text())
            pointer["phase"] = "complete"
            paths[0].write_text(json.dumps(pointer))

    events, _ = _samples(fleet, seconds=121 + grace, grace=grace, change=finish)
    assert readings[:2] == [(0, True), (60, False)], readings
    assert counts["reviews"] == 0, dict(counts)
    assert len(events) == 1 and events[0]["event"] == follow.FOLLOWER_SETTLE_EVENT
    assert clock.now == 120 + grace, (
        "stale emptiness must receive a full fallback interval of grace"
    )


def test_fresh_station_answer_bypasses_cached_fallback(fleet):
    clock, _, counts, _, _ = fleet

    def refresh(now, subscriber, paths):
        if now == 1:
            station_subscriber.record_live_work(
                subscriber.store,
                PROJECT,
                at=datetime.now(tz=UTC),
                project_live=False,
                unowned=False,
                sessions=[],
            )

    events, _ = _samples(fleet, seconds=10, change=refresh)
    assert len(events) == 1 and events[0]["event"] == follow.FOLLOWER_SETTLE_EVENT
    assert clock.now == 1
    assert counts["reviews"] == 0


def test_observed_populations_share_one_fallback_reading(fleet):
    path = fleet[4][0]
    pointer = json.loads(path.read_text())
    pointer["session"] = "peer-session"
    path.write_text(json.dumps(pointer))
    events, sampled = _samples(fleet, seconds=60, observed=("peer-session",))
    counts = fleet[2]
    assert len(sampled) == 60
    assert counts["populations"] <= 2, dict(counts)
    assert not events
