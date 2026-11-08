"""A station sweep's review launch reaches its owning follower once."""

# ruff: noqa: F811 - the imported fixture is also a test parameter.

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import (
    recovery_review_acceptance,
    recovery_review_dispatch,
    resumption,
    runs,
    station,
    station_obligations,
    station_store,
    station_subscriber,
    ticker,
)
from tests.test_scoring_dispatches_its_review import (
    CONFIG,
    _scoring_pointer,
    isolated_project,  # noqa: F401 - fixture
)

PROJECT = "sample"
SESSION = "session-orchestrating"


def test_a_sweep_review_reaches_its_session_on_the_next_publish(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home, repo = isolated_project
    source = _scoring_pointer(config_home, repo, "r-awaiting-review")
    dispatched: list[dict] = []
    launched: list[Path] = []
    real_dispatch = recovery_review_dispatch.dispatch_review_for_run

    def dispatch(*args, **kwargs):
        result = real_dispatch(*args, **kwargs)
        dispatched.append(result)
        return result

    def launch(plan, *, log_path, stderr_path, prompt_path):
        launched.append(log_path)
        log_path.write_text('{"type":"system","subtype":"init"}\n')
        return os.getpid()

    def sweep(project, **kwargs):
        return resumption.sweep(project, config=CONFIG, launcher=launch, **kwargs)

    monkeypatch.setattr(recovery_review_acceptance, "dispatch_review_for_run", dispatch)
    # Obligations export is independent of the transition publication under test.
    monkeypatch.setattr(station_obligations, "publish", lambda *args, **kwargs: None)
    runtime = tmp_path / "station"
    moment = datetime.now(tz=UTC)
    with (
        station_store.StationStore(runtime) as writer,
        station_store.StationStore(runtime) as reader,
    ):
        active = station.Station(
            writer,
            home=runs.crew_home(),
            projects=lambda: {PROJECT: repo / "docs"},
            sweeps=station.SweepBeat(sweep=sweep, start=lambda work: work()),
        )
        own = station_subscriber.StationSubscriber(
            reader, PROJECT, session=SESSION, clock=moment.timestamp
        )
        peer = station_subscriber.StationSubscriber(
            reader, PROJECT, session="another-session", clock=moment.timestamp
        )
        active.tick(now=moment)
        assert len(dispatched) == len(launched) == 1, dispatched
        assert dispatched[0]["dispatched"] is True, dispatched
        review_id = dispatched[0]["review_run_id"]
        review = runs.read_pointer(review_id)
        assert review["session"] == SESSION
        assert review["role"] == "review"
        # The launched worker publishes its phase before the station observes it.
        review["phase"] = "working"
        runs._write_json(runs.pointer_path(review_id), review)
        assert own.poll() == peer.poll() == []

        active.tick(now=moment + timedelta(seconds=1))
        verdict = writer.classifications()[review_id]
        rows = reader.events_for(project=PROJECT, audiences=(SESSION,), after_id=0)
        working = [
            row
            for row in rows
            if row["run"] == review_id and row["to_state"] == "working"
        ]
        assert verdict["state"] == "working", verdict
        assert len(working) == 1, (
            f"review classified {verdict['state']}; "
            f"working transition rows={len(working)}; rows={rows}"
        )
        assert working[0]["from_state"] == "dispatched"
        assert working[0]["kind"] == "transition"
        # A review launch is presented under the run being reviewed.
        delivered = own.poll()
        assert any(
            row.get("review_run_id") == review_id
            and row["run_id"] == source["run_id"]
            and row["to_state"] == ticker.IN_REVIEW_STATE
            for row in delivered
        ), delivered
        assert peer.poll() == []
        assert own.poll() == []
        active.tick(now=moment + timedelta(seconds=2))
        assert own.poll() == []
        assert len(dispatched) == 1


def test_an_existing_working_run_is_inventory_on_station_start(
    home: Path, tmp_path: Path
) -> None:
    record = {
        "run_id": "r-already-working",
        "project": PROJECT,
        "session": SESSION,
        "phase": "working",
        "pid": os.getpid(),
        "node": {"id": "already-working"},
    }
    runs._write_json(runs.pointer_path(record["run_id"]), record)
    with station_store.StationStore(tmp_path / "station") as store:
        active = station.Station(
            store,
            home=runs.crew_home(),
            projects=dict,
            sweeps=station.SweepBeat(
                sweep=lambda *args, **kwargs: {}, start=lambda f: f()
            ),
        )
        active.tick()
        assert store.classifications()[record["run_id"]]["state"] == "working"
        assert store.events() == []
