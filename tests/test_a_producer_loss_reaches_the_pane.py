"""A follower reports an aged publication without a prompt or a run event."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import crew_follow_commands as follow
from reckon.crew import obligation_snapshot, runs, station_subscriber

PROJECT = "snapshot-fixture"
SESSION = "snapshot-session"
INSTANT = datetime(2026, 11, 1, tzinfo=UTC)
BOUND = obligation_snapshot.FRESHNESS_WINDOW_SECONDS
SAMPLE = obligation_snapshot.STAT_IDENTITY_INTERVAL_SECONDS


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(runs, "renew_producer_lease", lambda *args: None)
    monkeypatch.setattr(
        runs,
        "follower_owner",
        lambda: (os.getpid(), runs._process_start_time(os.getpid())),
    )
    clock = Clock()
    monkeypatch.setattr(
        obligation_snapshot, "_utc_now", lambda: INSTANT + timedelta(seconds=clock())
    )
    return clock


def _publish(clock, age):
    document = {"computed_at": (INSTANT + timedelta(seconds=clock() - age)).isoformat()}
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)
    assert (
        obligation_snapshot.snapshot_age_seconds(
            obligation_snapshot.read_snapshot(PROJECT, SESSION)
        )
        == age
    )


def _stub_subscriber():
    # Use the subscriber's real wait loop; the store alone is replaced.
    rows = iter([[{"event": "fixture-row", "project": PROJECT}], [], [], [], []])
    stub = SimpleNamespace(history=[], path=SimpleNamespace(reported={}))
    stub.poll = lambda: next(rows, [])
    stub.flush = list
    stub.wake = lambda **kwargs: station_subscriber.StationSubscriber.wake(
        stub, **kwargs
    )
    return stub


def _drive(clock, monkeypatch, *, mode="subscriber", samples=4, restore_at=None):
    stop = threading.Event()
    monkeypatch.setattr(runs, "producer_live", lambda project: mode == "stream")
    stream = runs.watch_stream_path(PROJECT)
    if mode == "stream":
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_text("")
        monkeypatch.setattr(
            runs,
            "watch_stream_cursor",
            lambda project: {
                "producer": {},
                "stream_path": str(stream),
                "baseline": [],
                "offset": 0,
            },
        )

    def sleep(_interval):
        clock.value += SAMPLE
        if clock() == restore_at:
            _publish(clock, 0)
        if clock() >= samples * SAMPLE:
            stop.set()

    return [
        (clock(), event)
        for event in follow._follow_watch_lines(
            PROJECT,
            session=SESSION,
            subscriber=_stub_subscriber() if mode == "subscriber" else None,
            clock=clock,
            sleeper=sleep,
            poll_interval=SAMPLE,
            stop=stop,
            sweep=None,
        )
    ]


@pytest.mark.parametrize("mode", ["subscriber", "absent", "stream"])
def test_stale_snapshot_reaches_the_pane_once_then_restores(
    isolated, monkeypatch, mode
):
    clock = isolated
    _publish(clock, int(BOUND + 60))
    received = _drive(clock, monkeypatch, mode=mode, restore_at=2 * SAMPLE)
    if mode == "subscriber":
        assert any(event["event"] == "fixture-row" for _, event in received)
    notices = [
        (at, event) for at, event in received if event["event"].startswith("producer-")
    ]
    assert [event["event"] for _, event in notices] == [
        "producer-lost",
        "producer-restored",
    ]
    assert [at for at, _ in notices] == [0, 2 * SAMPLE]
    lost = notices[0][1]
    assert lost["project"] == PROJECT
    assert lost["session"] == SESSION
    assert lost["age_seconds"] == BOUND + 60
    assert lost["bound_seconds"] == BOUND
    assert lost["remedy"] == runs.watcher_ensure_line(PROJECT)
    assert all(
        str(value) in lost["line"]
        for value in (PROJECT, int(BOUND + 60), lost["remedy"])
    )
    restored = notices[1][1]
    assert restored["project"] == PROJECT and restored["age_seconds"] == 0
    assert PROJECT in restored["line"]


def test_a_continuing_loss_repeats_only_after_its_bound(isolated, monkeypatch):
    _publish(isolated, int(BOUND + 1))
    received = _drive(isolated, monkeypatch, samples=int(BOUND / SAMPLE) + 2)
    lost = [(at, event) for at, event in received if event["event"] == "producer-lost"]
    assert [at for at, _ in lost] == [0, BOUND]


@pytest.mark.parametrize("age", [0, int(BOUND)])
def test_a_snapshot_inside_its_bound_emits_no_loss(isolated, monkeypatch, age):
    _publish(isolated, age)
    received = _drive(isolated, monkeypatch, samples=1)
    assert [event["event"] for _, event in received] == ["fixture-row"]


@pytest.mark.parametrize("json_output", [False, True])
def test_snapshot_notices_use_the_followers_existing_output_channel(
    isolated, monkeypatch, json_output
):
    events = [
        {"event": kind, "project": PROJECT, "session": SESSION, "line": line}
        for kind, line in (
            ("producer-lost", "producer snapshot-fixture lost; re-arm with the remedy"),
            ("producer-restored", "producer snapshot-fixture restored"),
            (
                "producer-slow",
                "producer snapshot-fixture slow: its tick has run 9m0s, "
                "so no restart is needed",
            ),
        )
    ]
    monkeypatch.setattr(follow, "_follow_watch_lines", lambda *a, **kw: iter(events))
    monkeypatch.setattr(follow, "_hand_off_from_follower", lambda *a, **kw: False)
    result = CliRunner().invoke(
        follow.crew,
        ["follow", "--project", PROJECT, "--session", SESSION, "--no-color"]
        + (["--json"] if json_output else []),
    )
    assert result.exit_code == 0, result.output
    for event in events:
        assert event["line"] in result.stdout
    # A notice names no run, so it is never painted as a run's transition.
    assert "unknown" not in result.stdout, result.stdout
    assert not result.stderr


def test_a_quiet_stations_publication_is_judged_by_its_heartbeat(isolated, monkeypatch):
    clock = isolated
    _publish(clock, int(BOUND + 60))
    document = {
        "project": PROJECT,
        "computed_at": (INSTANT - timedelta(days=1)).isoformat(),
        "obligations": [],
    }
    path = obligation_snapshot.station_snapshot_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "station": {"heartbeat": INSTANT.isoformat()},
                "sessions": {SESSION: document},
            }
        )
    )
    reading = obligation_snapshot.read_station_answer(PROJECT, SESSION)
    assert reading["heartbeat_age_seconds"] == 0 and not reading["stale"]
    received = _drive(clock, monkeypatch, samples=2)
    assert [event["event"] for _, event in received] == ["fixture-row"]


def test_a_missing_publication_does_not_invent_an_age_or_a_recovery(
    isolated, monkeypatch
):
    received = _drive(isolated, monkeypatch, samples=2)
    assert [event["event"] for _, event in received] == ["fixture-row"]


def test_a_fresh_producer_snapshot_supersedes_a_stale_station_export(
    isolated, monkeypatch
):
    clock = isolated
    age = int(BOUND + 60)
    _publish(clock, age)
    document = {
        "project": PROJECT,
        "computed_at": (INSTANT - timedelta(seconds=age)).isoformat(),
        "obligations": [],
    }
    path = obligation_snapshot.station_snapshot_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "station": {"heartbeat": document["computed_at"]},
                "sessions": {SESSION: document},
            }
        )
    )
    assert obligation_snapshot.read_station_answer(PROJECT, SESSION)["stale"]
    received = _drive(clock, monkeypatch, restore_at=2 * SAMPLE)
    assert [
        event["event"]
        for _, event in received
        if event["event"].startswith("producer-")
    ] == ["producer-lost", "producer-restored"]


@pytest.mark.timeout(2)
def test_an_exhausted_subscriber_ends_the_follower(isolated):
    subscriber = _stub_subscriber()
    subscriber.wake = lambda **kwargs: iter(())
    assert (
        list(
            follow._follow_watch_lines(
                PROJECT, session=SESSION, subscriber=subscriber, sweep=None
            )
        )
        == []
    )
