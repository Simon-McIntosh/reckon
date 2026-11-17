"""A held project input leaves other refreshes and their last answers usable."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
        station_obligations.publish(
            store,
            project=READY,
            home=home,
            changed=True,
            now=later,
            submit=beat.request,
        )
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
        store.close()
