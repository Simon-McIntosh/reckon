"""The station runs the review reflex and settle detection once, for every session.

A follower used to run the recovery sweep on its own cadence, so a session with
three panes paid for the same fleet-wide reads three times and settled each pane
from its own directory listing. The station is the node's one reader, so both
move here: the sweep runs once per session in the station, on every change and
on the station's own beat, and a pane's settle is judged by a reading the
station writes on every tick.

Each property is argued beside the failure it prevents. A sweep that runs in a
subscriber is invisible while one pane is attached and multiplies with the
panes, so the count is asserted across three subscribers. A beat that is not
kept leaves a hold or a settle window to expire with nothing to notice it, and
a resumed run that writes nothing is exactly the case that produces no change.
A settle reader that takes the sweep's own stamp dates a run resumed with
advice from whatever automatic resume it last had. A station that beats after
it publishes hands its first reader the previous beat, or none, and reads as
stopped while it starts.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import checkout_trust
from reckon import crew_follow_commands as follow_commands
from reckon.crew import (
    recovery_review_dispatch,
    station,
    station_obligations,
    station_store,
    station_subscriber,
)
from reckon.crew.run_paths import crew_home

THIS_HOST = socket.gethostname()
OTHER_HOST = "another-node"
PROJECT = "fixture"
SESSION = "session-one"
MOMENT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _pointer(
    home: Path,
    run_id: str,
    *,
    session: str | None = SESSION,
    phase: str = "working",
    launcher_host: str = THIS_HOST,
) -> Path:
    stream = home / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init"}) + "\n", encoding="utf-8"
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "phase": phase,
        "launcher_host": launcher_host,
        "pid": os.getpid(),
        "created_at": "2026-10-27T11:00:00Z",
        "log_path": str(stream),
        "worktree": str(home / "trees" / run_id),
        "node": {},
    }
    path = home / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class _Sweeps:
    """Every sweep the station asked for, run where the caller can see it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, project: str, *, session=None, **kwargs) -> dict:
        self.calls.append((project, str(session)))
        return {}


def _station(tmp_path: Path, home: Path, sweeps: _Sweeps) -> station.Station:
    store = station_store.StationStore(tmp_path / "station")
    beat = station.SweepBeat(sweep=sweeps, start=lambda run: run())
    return station.Station(store, home=home, projects=dict, sweeps=beat)


def _three_subscribers(store: station_store.StationStore):
    return [
        station_subscriber.StationSubscriber(store, PROJECT, session=SESSION)
        for _ in range(3)
    ]


def test_three_subscribers_share_one_sweep_run_in_the_station(
    home: Path, tmp_path: Path
) -> None:
    base = crew_home()
    _pointer(base, "r-one")
    sweeps = _Sweeps()
    node = _station(tmp_path, base, sweeps)
    subscribers = _three_subscribers(node.store)

    node.tick(now=MOMENT)
    assert sweeps.calls == [(PROJECT, SESSION)]

    # Attaching three panes to the one session adds no sweep of its own: what
    # a pane does is read the station's rows.
    for subscriber in subscribers:
        subscriber.poll()
    assert sweeps.calls == [(PROJECT, SESSION)]

    # A tick with nothing moved is not a change, so the beat bounds the next
    # sweep: before it elapses nothing runs, after it one sweep runs.
    node.tick(now=MOMENT + timedelta(seconds=1))
    assert sweeps.calls == [(PROJECT, SESSION)]
    node.tick(now=MOMENT + timedelta(seconds=station.SWEEP_BEAT_SECONDS + 1))
    assert sweeps.calls == [(PROJECT, SESSION), (PROJECT, SESSION)]
    for subscriber in subscribers:
        subscriber.poll()
    assert len(sweeps.calls) == 2


def test_a_session_launched_elsewhere_is_not_swept_here(
    home: Path, tmp_path: Path
) -> None:
    base = crew_home()
    _pointer(base, "r-here")
    _pointer(base, "r-away", session="session-two", launcher_host=OTHER_HOST)
    sweeps = _Sweeps()
    node = _station(tmp_path, base, sweeps)

    node.tick(now=MOMENT)

    assert sweeps.calls == [(PROJECT, SESSION)]


def test_a_resumed_run_waits_its_settle_window_from_the_newest_resume() -> None:
    now = datetime.now(tz=UTC)
    window = recovery_review_dispatch.REVIEW_SETTLE_SECONDS
    # Only advice resumed this run: the sweep's own stamp says nothing, and
    # reading it alone would call the run settled the moment it was resumed.
    advice = {
        "run_id": "r-one",
        recovery_review_dispatch.RESUME_SUPERSEDED_HEAD: {
            "at": now.isoformat(),
            "head": "a" * 40,
        },
    }
    remaining = recovery_review_dispatch.review_settle_seconds_remaining(advice)
    assert remaining > window - 5

    # An older automatic resume beside a newer advice resume dates the window
    # from the advice, because that is the head the run no longer carries.
    mixed = {
        **advice,
        "auto_resume": {"at": (now - timedelta(seconds=window + 60)).isoformat()},
    }
    assert recovery_review_dispatch.review_settle_seconds_remaining(mixed) > 0

    # A run resumed long enough ago is settled, whichever door resumed it.
    settled = {
        recovery_review_dispatch.RESUME_SUPERSEDED_HEAD: {
            "at": (now - timedelta(seconds=window + 1)).isoformat()
        }
    }
    assert recovery_review_dispatch.review_settle_seconds_remaining(settled) == 0.0


def test_a_quiet_resumed_run_is_swept_on_the_beats_clock(
    home: Path, tmp_path: Path
) -> None:
    base = crew_home()
    pointer = _pointer(base, "r-one")
    sweeps = _Sweeps()
    node = _station(tmp_path, base, sweeps)

    node.tick(now=MOMENT)
    assert len(sweeps.calls) == 1

    # The worker writes nothing after its resume: no pointer moves, no stream
    # grows, and nothing else in the fleet changes. The sweep still has to run
    # when the window passes, so the station evaluates it on the beat rather
    # than only when a change arrives.
    assert json.loads(pointer.read_text(encoding="utf-8"))["phase"] == "working"
    node.tick(now=MOMENT + timedelta(seconds=station.SWEEP_BEAT_SECONDS / 2))
    assert len(sweeps.calls) == 1
    node.tick(now=MOMENT + timedelta(seconds=station.SWEEP_BEAT_SECONDS + 1))
    assert len(sweeps.calls) == 2


def _reading(
    store: station_store.StationStore,
    *,
    project_live: bool,
    sessions: list[str] | None = None,
    unowned: bool = False,
) -> None:
    station_subscriber.record_live_work(
        store,
        PROJECT,
        at=datetime.now(tz=UTC),
        project_live=project_live,
        unowned=unowned,
        sessions=sessions or [],
    )


def _spawn_follower(
    events: list[dict],
    stop: threading.Event,
    *,
    subscriber: station_subscriber.StationSubscriber | None,
    sweeps=None,
):
    """Run one arming on a thread, as the session host runs a child."""

    def run() -> None:
        events.extend(
            follow_commands._follow_watch_lines(
                PROJECT,
                session=SESSION,
                poll_interval=0.01,
                settle=True,
                settle_interval=0.2,
                sweep=sweeps,
                stop=stop,
                subscriber=subscriber,
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _settles(events: list[dict]) -> list[dict]:
    return [
        event
        for event in events
        if event.get("event") == follow_commands.FOLLOWER_SETTLE_EVENT
    ]


def test_a_follower_reading_the_station_runs_no_sweep_of_its_own(
    home: Path, tmp_path: Path
) -> None:
    """The sweep a pane used to run on its cadence now runs only in the station."""
    base = crew_home()
    _pointer(base, "r-one")
    store = station_store.StationStore(tmp_path / "station")
    _reading(store, project_live=True, sessions=[SESSION])
    subscriber = station_subscriber.StationSubscriber(store, PROJECT, session=SESSION)

    panes = _Sweeps()
    from_station: list[dict] = []
    stop = threading.Event()
    thread = _spawn_follower(from_station, stop, subscriber=subscriber, sweeps=panes)
    try:
        time.sleep(1.0)
        assert panes.calls == [], "a pane reading the station swept beside it"
    finally:
        stop.set()
        thread.join(5.0)
    assert not thread.is_alive()

    # The positive control: the same recorder on the producer path still sees
    # the sweep the follower has always run, so a count of zero is a real
    # absence rather than a recorder that could never have been called.
    from_producer: list[dict] = []
    stop = threading.Event()
    thread = _spawn_follower(from_producer, stop, subscriber=None, sweeps=panes)
    try:
        time.sleep(1.0)
        assert panes.calls, "the producer path stopped sweeping"
    finally:
        stop.set()
        thread.join(5.0)
    assert not thread.is_alive()


def test_a_pane_settles_on_the_stations_reading_not_the_pointer_directory(
    home: Path, tmp_path: Path
) -> None:
    """The station's reading is the judgement, in both directions.

    A pane that read the pointer directory would decide the opposite of the
    reading in each direction here: it would stay open with no pointer at all
    and leave with a working one, which is exactly the listing the station
    replaces.
    """
    base = crew_home()
    store = station_store.StationStore(tmp_path / "station")
    _reading(store, project_live=True, sessions=[SESSION])
    subscriber = station_subscriber.StationSubscriber(store, PROJECT, session=SESSION)

    events: list[dict] = []
    stop = threading.Event()
    thread = _spawn_follower(events, stop, subscriber=subscriber, sweeps=_Sweeps())
    try:
        # No live pointer has ever been written, so a listing would call the
        # fleet drained and leave; the station reads work and the pane stays.
        time.sleep(0.5)
        assert _settles(events) == [], "a pane settled on a live station reading"

        # A working pointer is on disk, so a listing would keep the pane open;
        # the station reads the fleet drained and the pane settles.
        _pointer(base, "r-one")
        _reading(store, project_live=False)
        thread.join(20.0)
        assert not thread.is_alive(), "the pane did not leave on a drained reading"
        assert len(_settles(events)) == 1, _settles(events)
    finally:
        stop.set()
        thread.join(5.0)


def test_the_station_writes_the_reading_a_pane_settles_by(
    home: Path, tmp_path: Path
) -> None:
    base = crew_home()
    live = _pointer(base, "r-one")
    sweeps = _Sweeps()
    node = _station(tmp_path, base, sweeps)

    node.tick(now=MOMENT)
    store = node.store
    assert station_subscriber.live_work_reading(
        store, PROJECT, session=SESSION, now=MOMENT
    )
    assert station_subscriber.live_work_reading(
        store, PROJECT, session=None, now=MOMENT
    )
    # A session this project holds no live run for is not held open by one.
    assert not station_subscriber.live_work_reading(
        store, PROJECT, session="session-two", now=MOMENT
    )

    # An ownerless live pointer keeps every session's reading live, because
    # nothing shows the work is not the reader's own.
    record = json.loads(live.read_text(encoding="utf-8"))
    record["session"] = ""
    live.write_text(json.dumps(record), encoding="utf-8")
    node.tick(now=MOMENT)
    assert station_subscriber.live_work_reading(
        store, PROJECT, session="session-two", now=MOMENT
    )

    # A terminal run is work the coordinator owes, not work a pane is owed.
    record["session"] = SESSION
    record["phase"] = "complete"
    live.write_text(json.dumps(record), encoding="utf-8")
    node.tick(now=MOMENT)
    assert not station_subscriber.live_work_reading(
        store, PROJECT, session=SESSION, now=MOMENT
    )
    assert not station_subscriber.live_work_reading(
        store, PROJECT, session=None, now=MOMENT
    )

    # A reading nothing has refreshed is no verdict at all: the pane is already
    # told its station is stale, and settling it would end a pane nobody reads.
    assert not station_subscriber.live_work_reading(
        store,
        PROJECT,
        session=SESSION,
        now=MOMENT + timedelta(seconds=station_subscriber.STALE_HEARTBEAT_SECONDS + 1),
    )


def test_the_first_snapshot_carries_this_ticks_heartbeat(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = crew_home()
    _pointer(base, "r-one")
    sweeps = _Sweeps()
    node = _station(tmp_path, base, sweeps)
    seen: list[str | None] = []

    real_publish = station_obligations.publish

    def recording_publish(store, **kwargs):
        seen.append(store.station_value(station_store.HEARTBEAT_KEY))
        return real_publish(store, **kwargs)

    monkeypatch.setattr(station_obligations, "publish", recording_publish)

    node.tick(now=MOMENT)

    assert seen, "the station published no obligations snapshot"
    assert seen[0] == MOMENT.isoformat()


def test_a_reload_publishes_under_the_runtime_root_without_patching_the_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    reloader = station.StationReloader(runtime_root)
    reloader.identity = "0" * 40
    monkeypatch.setattr(
        checkout_trust, "source_code_identity", lambda *a, **k: "1" * 40
    )
    # The shared resolver answers with the station's own root, which is the
    # whole point: the reload writes where the station says the node keeps its
    # runtime state, and refuses a root it cannot prove local.
    monkeypatch.setattr(
        station_store, "runtime_root_source", lambda **kwargs: (runtime_root, "xdg")
    )
    image = runtime_root / "reload-trees" / "key" / ("1" * 40)
    image.mkdir(parents=True)
    resolved: list[Path] = []

    def materialize(root, revision, *, active_root=None):
        resolved.append(checkout_trust._committed_tree_cache())
        return image, [], [], os.open(os.devnull, os.O_RDONLY)

    monkeypatch.setattr(checkout_trust, "materialize_committed_tree", materialize)
    monkeypatch.setattr(checkout_trust, "reckon_imports", lambda root: ([], "none"))

    native_resolver = checkout_trust._committed_tree_cache
    reloader.poll()

    # The cache the tree is materialised under is the station's own runtime
    # root, answered by the shared resolver rather than by a patched module.
    assert resolved == [runtime_root / "reload-trees"]
    assert checkout_trust._committed_tree_cache is native_resolver
    assert reloader.deferred_identity == "1" * 40
