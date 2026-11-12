"""A slow station refresh does not hold the review reflex.

The station runs the review reflex, and the arm that decides a sweep is due
used to run only inside the tick. A tick that spends minutes in its
per-project refresh stage therefore held every review it would have armed,
which is how deliveries were reviewed six to ten minutes late against a
two-minute window. The arm now beats on a thread of its own, reading the live
fleet, so a run delivered while the tick is held is still reviewed inside the
window.

Each case is argued beside the failure it prevents. The first delivers a run
while a tick is held in its slow stage and measures how long the reflex takes
to reach the dispatch. The second has the reflex unable to run at all and
asserts that the overdue duty names the stage that held it, its age, and the
command that dispatches the review, because a coordinator cannot act on
"inspect the follower's review pass".

Every case runs on a temporary configuration home and a temporary station
store, so no reader reaches the operator's own crew home or this node's
station.
"""

from __future__ import annotations

import importlib
import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import (
    obligation_snapshot,
    resumption,
    runs,
    station,
    station_obligations,
    station_store,
)
from reckon.crew.run_paths import crew_home
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "fixture"
SESSION = "session-one"
REVIEW_PROJECT = "review-owner-fixture"
REVIEW_SESSION = "coordinator-fixture"
WINDOW = 8.0


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _pointer(home: Path, run_id: str) -> Path:
    """One live pointer whose run has a manifest, so a delivery is visible."""
    stream = home / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init"}) + "\n", encoding="utf-8"
    )
    manifest = home / "runs" / run_id / "manifest.md"
    manifest.write_text("node: fixture\nstatus: complete\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "working",
        "launcher_host": socket.gethostname(),
        "manifest_path": str(manifest),
        "worktree": str(home / "trees" / run_id),
        "node": {},
    }
    path = home / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class _Dispatches:
    """Every run the reflex asked to dispatch, with the instant it asked.

    The run-level dispatch is recorded in place of composing and launching a
    review: this case measures how long the reflex takes to reach that
    dispatch while the tick is held, and the dispatch itself is proved by the
    reflex's own cases.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[float, list[str]]] = []
        self.lock = threading.Lock()

    def __call__(self, *, project=None, pointers=None, **kwargs) -> dict:
        ids = [str(pointer.get("run_id") or "") for pointer in (pointers or ())]
        with self.lock:
            self.calls.append((time.monotonic(), ids))
        return {"dispatched": ids}

    def delivered(self, run_id: str) -> list[float]:
        with self.lock:
            return [at for at, ids in self.calls if run_id in ids]


def _station(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reflex_beat_seconds: float,
    sweeps: station.SweepBeat,
) -> station.Station:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return station.Station(
        station_store.StationStore(runtime),
        home=crew_home(),
        projects=dict,
        sweeps=sweeps,
        reflex_beat_seconds=reflex_beat_seconds,
    )


def test_a_run_delivered_into_a_held_refresh_is_reviewed_in_the_window(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run delivered while the tick is held in its slow stage is reviewed.

    The station runs through ``run_station``, the entry point a real station
    takes, so the reflex's arm is started and stopped by production code
    rather than by this case.
    """
    monkeypatch.setattr(resumption, "DEFAULT_SWEEP_SECONDS", WINDOW)
    dispatches = _Dispatches()
    monkeypatch.setattr(resumption, "dispatch_awaiting_reviews", dispatches)
    sweeps = station.SweepBeat(
        sweep=resumption.sweep,
        beat_seconds=WINDOW,
        start=lambda run: run(),
    )
    node = _station(
        tmp_path,
        monkeypatch,
        reflex_beat_seconds=0.02,
        sweeps=sweeps,
    )
    gate = threading.Event()
    entered = threading.Event()
    finished = threading.Event()

    def held_publish(*args, **kwargs):
        entered.set()
        gate.wait(timeout=20)

    monkeypatch.setattr(station_obligations, "publish", held_publish)

    def run_tick() -> None:
        station.run_station(station_instance=node, store=node.store, once=True)
        finished.set()

    # A run already live when the tick opens is what gives the tick a project.
    _pointer(crew_home(), "run-already-live")
    try:
        ticker = threading.Thread(target=run_tick, name="held-tick", daemon=True)
        ticker.start()
        assert entered.wait(timeout=10), "the tick never entered its refresh stage"
        assert (
            node.store.station_value(obligation_snapshot.STATION_TICK_STAGE_KEY)
            == "per-project-refresh"
        )
        delivered_at = time.monotonic()
        _pointer(crew_home(), "run-into-a-slow-refresh")
        deadline = delivered_at + WINDOW
        while time.monotonic() < deadline and not dispatches.delivered(
            "run-into-a-slow-refresh"
        ):
            time.sleep(0.005)
        seen = dispatches.delivered("run-into-a-slow-refresh")
        assert seen, (
            "the reflex did not reach the delivered run's review while the "
            "tick was held in its per-project refresh stage"
        )
        assert seen[0] - delivered_at <= WINDOW
        assert not finished.is_set(), (
            "the tick finished before the review was dispatched, so this case "
            "did not hold the refresh stage across the dispatch"
        )
    finally:
        gate.set()
        ticker.join(timeout=30)


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / REVIEW_PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _commit(root, "seed.txt", "seed\n")
    config_home = crew_home()
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "mounts.json").write_text(
        json.dumps({REVIEW_PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def test_an_overdue_review_names_the_stage_that_held_it(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the window the duty names the stage, its age and the dispatch."""
    run_id = "run-slow-station"
    head = _commit(repository, "reckon/repair_module.py", "REPAIR = 1\n")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: slow-station\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": REVIEW_PROJECT,
        "session": REVIEW_SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": "slow-station",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    store = station_store.StationStore(runtime)
    held_for = 185
    store.set_station_value(
        obligation_snapshot.STATION_TICK_STAGE_KEY, "per-project-refresh"
    )
    store.set_station_value(
        obligation_snapshot.STATION_TICK_STARTED_AT_KEY,
        (_now() - timedelta(seconds=held_for)).isoformat(),
    )
    store.set_station_value(
        obligation_snapshot.STATION_TICK_FINISHED_AT_KEY,
        (_now() - timedelta(seconds=held_for + 60)).isoformat(),
    )
    store.write_obligation(
        REVIEW_SESSION,
        {"document": {"project": REVIEW_PROJECT, "session": REVIEW_SESSION}},
        changed_at=_now(),
    )

    command = (
        f"reckon crew dispatch --project {REVIEW_PROJECT} --role review --run {run_id}"
    )
    window = obligations_module._reflex_review_window_seconds()
    row = {
        "run_id": run_id,
        "node": "slow-station",
        "plan": "fixture-plan",
        "classification": "scoring",
        "age_seconds": window + 60,
        "head": head,
        "next_action": command,
    }

    with runs.follower_claim(REVIEW_PROJECT, REVIEW_SESSION, delivery="stream"):
        item = obligations_module._review_duty_item(
            REVIEW_PROJECT,
            row,
            pointer,
            now=_now(),
            grace=60,
            live_pointers=[pointer],
            placements={},
        )

    assert item is not None
    assert item["kind"] == "review-missing", item
    overdue = str(item["next_command"])
    assert "per-project-refresh" in overdue, item
    assert f"{held_for // 60}m {held_for % 60}s" in overdue, item
    assert command in overdue, item
    assert "inspect the follower" not in overdue, item
