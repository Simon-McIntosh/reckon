"""Every run a station tick classifies shares one listing of each review store.

Classifying a run reads the review store several times: its index, the run's
own records and the records its review's inputs are keyed on. The producer's
pass shares one enumeration of each store across every run it classifies; the
station's classify stage must do the same, or each live run repeats the
selection and the listing and the tick grows with the run count times the store.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from reckon.crew import recovery_watch, review_records, station_store
from reckon.crew.run_paths import crew_home

STALL_SECONDS = 60


def _pointer(home: Path, run_id: str) -> None:
    stream = home / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "s"}) + "\n"
    )
    tree = home / "trees" / run_id
    tree.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "project": "fixture",
        "session": "s",
        "phase": "working",
        "launcher_host": socket.gethostname(),
        "pid": os.getpid(),
        "created_at": "2026-10-24T12:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(home / "runs" / run_id / "manifest.md"),
        "worktree": str(tree),
        "repo": str(tree),
        "node": {},
    }
    path = home / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


@pytest.fixture()
def scopes(home: Path, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    base = crew_home()
    for run_id in ("r-one", "r-two", "r-three"):
        _pointer(base, run_id)
    seen: list[object] = []
    real = recovery_watch.classify_pointer

    def recording(record, **kwargs):
        seen.append(review_records._TICK_REVIEW_STORES.get())
        return real(record, **kwargs)

    monkeypatch.setattr(recovery_watch, "classify_pointer", recording)
    return seen


def test_every_run_in_a_tick_shares_one_store_scope(home, scopes, tmp_path):
    store = station_store.StationStore(tmp_path / "station")
    store.tick(home=crew_home())
    store.classify_tick(stall_seconds=STALL_SECONDS)
    assert len(scopes) == 3
    assert all(scope is not None for scope in scopes)
    assert len({id(scope) for scope in scopes}) == 1


def test_a_later_tick_opens_a_fresh_scope(home, scopes, tmp_path):
    store = station_store.StationStore(tmp_path / "station")
    store.tick(home=crew_home())
    store.classify_tick(stall_seconds=STALL_SECONDS)
    first = scopes[0]
    scopes.clear()
    for run_id in ("r-one", "r-two", "r-three"):
        path = crew_home() / "live" / f"{run_id}.json"
        record = json.loads(path.read_text())
        record["last_heartbeat"] = "2026-10-24T12:30:00Z"
        path.write_text(json.dumps(record))
    store.tick(home=crew_home())
    store.classify_tick(stall_seconds=STALL_SECONDS)
    assert scopes
    assert all(scope is not None and scope is not first for scope in scopes)
    assert review_records._TICK_REVIEW_STORES.get() is None
