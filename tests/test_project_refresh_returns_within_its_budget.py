"""A held project input leaves other refreshes and their last answers usable."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import (
    obligation_snapshot,
    runs,
    station,
    station_obligations,
    station_store,
)
from tests.helpers.git import git

SLOW = "alpha-fixture"
READY = "zeta-fixture"
MOMENT = datetime(2026, 11, 16, tzinfo=UTC)


def _session(project: str) -> str:
    return f"session-{project}"


@pytest.fixture()
def projects(home, tmp_path, monkeypatch):
    docs = tmp_path / "repository" / "docs"
    docs.mkdir(parents=True)
    (docs.parent / "seed").write_text("fixture\n")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "seed"),
        ("commit", "-q", "-m", "test: seed project refresh fixture"),
    ):
        git(docs.parent, *args)
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({SLOW: str(docs), READY: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setattr(
        station_obligations,
        "follower_rows",
        lambda project: [{"session": _session(project), "live": True}],
    )
    for project in (SLOW, READY):
        manifest = tmp_path / f"{project}.md"
        manifest.write_text(
            f"node: {project}\nstatus: blocked\nblockers: fixture input\n"
        )
        path = runs.pointer_path(f"run-{project}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": f"run-{project}",
                    "project": project,
                    "session": _session(project),
                    "repo": str(docs.parent),
                    "worktree": str(docs.parent),
                    "base_sha": git(docs.parent, "rev-parse", "HEAD"),
                    "process_alive": False,
                    "role": "implement",
                    "manifest_path": str(manifest),
                    "node": {
                        "id": project,
                        "plan": "fixture",
                        "section": "work",
                        "time_budget": "20m",
                        "write_paths": ["seed"],
                    },
                }
            )
        )
    return {SLOW: docs, READY: docs}


def _document(store, project):
    return station_obligations.stored_document(store.read_obligation(_session(project)))


def _wait_until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.mark.timeout(45)
def test_a_held_input_does_not_hold_a_finished_project(
    home: Path, tmp_path: Path, projects, monkeypatch
):
    store = station_store.StationStore(tmp_path / "station")
    beat = station.DutyBeat(store.root)
    node = station.Station(
        store,
        home=home,
        projects=lambda: projects,
        duties=beat,
        sweeps=SimpleNamespace(request=lambda *args, **kwargs: False),
    )
    entered, release = threading.Event(), threading.Event()
    original = station_obligations._ended_sweep_failures
    try:
        for project in projects:
            station_obligations.publish(
                store, project=project, home=home, changed=True, now=MOMENT
            )
        previous = dict(_document(store, SLOW))
        assert previous["obligations"], "the positive control must hold real duties"
        # A persisted sweep failure makes pruning read the project's input.
        store.set_station_value(
            station_obligations._sweep_failure_key(SLOW),
            json.dumps(
                {_session(SLOW): [{"run_id": f"run-{SLOW}", "error": "fixture input"}]}
            ),
        )

        def held(failures, *, project):
            if project == SLOW:
                entered.set()
                assert release.wait(30), "the case did not release the held input"
            return original(failures, project=project)

        monkeypatch.setattr(station_obligations, "_ended_sweep_failures", held)
        later = MOMENT + timedelta(seconds=180)
        started = time.monotonic()
        station_obligations.publish(
            store, project=SLOW, home=home, changed=True, now=later, submit=beat.request
        )
        assert entered.wait(5), "the input hold never reached production pruning"
        node.tick(now=later)
        stage_seconds = time.monotonic() - started
        assert stage_seconds < 10, f"refresh stage took {stage_seconds:.3f}s"
        assert _wait_until(lambda: READY not in beat.in_flight(), 10 - stage_seconds), (
            "held input kept the finished project unreported past 10 s"
        )
        elapsed = time.monotonic() - started
        assert elapsed < 10
        state = obligation_snapshot.fleet_state(READY, now=later)
        reference = obligation_snapshot.payload_for(state, _session(READY))
        actual = obligation_snapshot.live_payload(_document(store, READY), now=later)
        assert actual == reference
        assert actual["obligations"], "the cold comparison must compare real duties"
        pending = _document(store, SLOW)
        assert pending["obligations"] == previous["obligations"]
        assert pending["stale"]["age_seconds"] == 180
        assert pending["stale"]["stored_at"] == MOMENT.timestamp()
        assert pending["stale"]["reason"]
        exported = json.loads(
            obligation_snapshot.station_snapshot_path(SLOW, home=home).read_text()
        )
        assert exported["sessions"][_session(SLOW)] == pending
        assert beat.in_flight() == {SLOW}
        print(
            f"refresh stage={stage_seconds:.3f}s finished publication={elapsed:.3f}s stale age=180s cold parity=equal"
        )
    finally:
        release.set()
        beat.drain(timeout=10)
        assert not beat.in_flight(), "a project worker outlived the case"
        if release.is_set() and _document(store, SLOW) is not None:
            assert "stale" not in _document(store, SLOW)
        store.close()


@pytest.mark.parametrize("has_previous", [True, False])
@pytest.mark.timeout(30)
def test_a_held_derivation_retains_only_a_real_last_answer(
    home, tmp_path, projects, monkeypatch, has_previous
):
    # Successful temporary directories can be reused, while process memos keep
    # their path keys. Each scenario therefore owns distinct store/export keys.
    scenario = "retained" if has_previous else "unread"
    home = home / scenario
    store = station_store.StationStore(tmp_path / scenario / "station")
    beat = station.DutyBeat(store.root)
    entered, release = threading.Event(), threading.Event()
    original = obligation_snapshot.fleet_state
    try:
        if has_previous:
            station_obligations.publish(
                store, project=SLOW, home=home, changed=True, now=MOMENT
            )
        previous = _document(store, SLOW)
        previous_row = store.read_obligation(_session(SLOW))
        if previous is not None:
            payload = obligation_snapshot.live_payload(previous, now=MOMENT)
            monkeypatch.setattr(
                obligation_snapshot, "payload_for", lambda *args: payload
            )

        def held(project, **kwargs):
            if project == SLOW:
                entered.set()
                assert release.wait(20), "the case did not release the derivation"
            return original(project, **kwargs)

        monkeypatch.setattr(obligation_snapshot, "fleet_state", held)
        later = MOMENT + timedelta(seconds=20)
        store.heartbeat(now=later)
        station_obligations.publish(
            store, project=SLOW, home=home, changed=True, now=later, submit=beat.request
        )
        assert entered.wait(5)
        pending = _document(store, SLOW)
        path = obligation_snapshot.station_snapshot_path(SLOW, home=home)
        if has_previous:
            assert pending["obligations"] == previous["obligations"]
            assert pending["stale"]["age_seconds"] == 20
            assert pending["stale"]["key"] == station_obligations.stored_identity(
                store.read_obligation(_session(SLOW))
            )
            assert json.loads(path.read_text())["sessions"][_session(SLOW)] == pending
        else:
            assert pending is None, "a first refresh has not yet read the project"
            assert obligation_snapshot.snapshot_age_seconds(pending, now=later) is None
            assert not path.exists(), (
                "an unfinished first refresh published an empty answer"
            )
            reader = {"XDG_RUNTIME_DIR": str(tmp_path / scenario / "reader")}
            assert (
                obligation_snapshot.read_station_answer(
                    SLOW, _session(SLOW), now=later, environ=reader, home=home
                )
                is None
            )
            started = time.monotonic()
            station_obligations.publish(
                store,
                project=READY,
                home=home,
                changed=True,
                now=later,
                submit=beat.request,
            )
            assert _wait_until(lambda: READY not in beat.in_flight())
            exported = obligation_snapshot.station_snapshot_path(READY, home=home)
            assert exported.is_file(), "a finished project must export independently"
            answer = obligation_snapshot.read_station_answer(
                READY, _session(READY), now=later, environ=reader, home=home
            )
            assert answer is not None and answer["payload"]["obligations"]
            assert answer["stale"] is False
            assert _document(store, SLOW) is None
            assert not path.exists()
            assert not release.is_set()
            assert beat.in_flight() == {SLOW}
            print(
                f"first read: no answer, no age; ready project exported in "
                f"{time.monotonic() - started:.3f}s while the first input stayed held"
            )
        release.set()
        beat.drain(timeout=10)
        assert not beat.in_flight()
        completed = _document(store, SLOW)
        assert completed["obligations"]
        assert "stale" not in completed
        if has_previous:
            current_row = store.read_obligation(_session(SLOW))
            assert station_obligations.stored_identity(
                current_row
            ) == station_obligations.stored_identity(previous_row)
            assert current_row["changed_at"] == previous_row["changed_at"]
        before = store.read_obligation(_session(SLOW))
        export_stamp = path.stat().st_mtime_ns
        station_obligations.publish(
            store,
            project=SLOW,
            home=home,
            changed=False,
            now=later + timedelta(seconds=1),
        )
        assert store.read_obligation(_session(SLOW)) == before
        assert path.stat().st_mtime_ns == export_stamp
    finally:
        release.set()
        beat.drain(timeout=10)
        store.close()
