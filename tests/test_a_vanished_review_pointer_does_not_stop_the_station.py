"""A review pointer that vanishes under a read costs nothing else.

The station derives its view over a snapshot of the live pointers and then
reads each listed review's own pointer from disk. A review promoted or
discarded during a slow tick is therefore listed but no longer readable, and
the reader that expected a falsy value for it must see one rather than an
exception. A tick that fails for any other reason costs that tick alone: the
station prints the failure and goes on to the next one, and only a lost lease
ends the process.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import obligations, runs, station, station_store
from reckon.crew.host_lease import HostLease, local_host
from reckon.crew.node import CrewError
from tests.helpers.git import git as _git

subject_module = importlib.import_module("reckon.crew.recovery_review_subject")

PROJECT = "vanishing-review-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

SOURCE = "run-reviewed"
REVIEW = "run-live-review"


def _write_pointer(run_id: str, record: dict) -> None:
    runs._write_json(runs.pointer_path(run_id), record)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the vanishing-review fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations, "_utc_now", lambda: OBSERVED_AT)
    return root


def _source_pointer(repository: Path) -> dict:
    """The reviewed run's own pointer, owed a review at its current head."""
    _write_pointer(
        SOURCE,
        {
            "run_id": SOURCE,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "repo": str(repository),
            "worktree": str(repository),
            "node": {
                "id": "source-node",
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )
    return runs.read_pointer(SOURCE)


def _live_review_pointer(paths: list[str]) -> dict:
    record = {
        "run_id": REVIEW,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": True,
        "node": {
            "id": "coordinator-own-review",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": list(paths),
        },
    }
    _write_pointer(REVIEW, record)
    return dict(record)


def test_a_review_pointer_that_vanishes_between_listing_and_read_is_not_in_flight(
    repository: Path,
) -> None:
    """A review listed by the view's snapshot but gone from disk is not working.

    The snapshot is published directly, which is what puts the review in the
    derivation after its file has gone — the exact ordering a review promoted
    during a slow tick produces.
    """
    source = _source_pointer(repository)
    fields = subject_module._review_dispatch_fields(source)
    paths = list(fields["write_paths"])
    assert str(fields.get("head") or "") and len(paths) == 2, fields
    review = _live_review_pointer(paths)

    token = obligations._VIEW_LIVE_POINTERS.set({PROJECT: (dict(source), dict(review))})
    try:
        # Control: with the pointer present the same read finds the review in
        # flight, so the case below is about the vanished file and not about a
        # fixture the reader never reaches.
        assert obligations._current_review_in_flight(source) is True

        runs.pointer_path(REVIEW).unlink()
        assert not runs.pointer_path(REVIEW).exists()

        assert obligations._current_review_in_flight(source) is False
    finally:
        obligations._VIEW_LIVE_POINTERS.reset(token)


def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "mounts.json"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    (tmp_path / "mounts.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        station, "StationReloader", lambda _root: SimpleNamespace(poll=lambda: None)
    )
    return runtime


def _ticking_station(tick) -> SimpleNamespace:
    """A station instance whose only live part is the tick under this case."""
    return SimpleNamespace(
        clock=lambda: datetime.now(tz=UTC),
        reflex=SimpleNamespace(start=lambda: None, stop=lambda: None),
        tick=tick,
    )


def test_a_tick_that_raises_costs_one_tick_and_the_station_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing read in one tick is printed and the next tick still runs."""
    runtime = _runtime(tmp_path, monkeypatch)
    store = station_store.StationStore(runtime)
    lease = HostLease(runtime, "station", local_host(), os.getpid(), "fixture-job")
    ticks: list[int] = []

    def tick() -> None:
        ticks.append(len(ticks) + 1)
        if len(ticks) == 1:
            raise CrewError("no live run 'run-live-review'")
        # The second tick is the evidence the loop went on; the station is
        # stopped the way a service is, by the signal it installs for.
        signal.raise_signal(signal.SIGTERM)

    station.run_station(
        store=store,
        station_instance=_ticking_station(tick),
        lease=lease,
        interval=0.0,
        beat_seconds=5.0,
        renew_seconds=5.0,
    )

    assert ticks == [1, 2], ticks
    err = capsys.readouterr().err
    assert "station-tick-failure" in err, err
    assert "no live run 'run-live-review'" in err, err
    # The recorded traceback is what a reader uses to find the failing read.
    assert "CrewError" in err, err


def test_a_lost_lease_still_ends_the_station(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost lease is the one failure that ends the process, as before."""
    runtime = _runtime(tmp_path, monkeypatch)
    store = station_store.StationStore(runtime)
    lease = HostLease(runtime, "station", local_host(), os.getpid(), "fixture-job")

    def tick() -> None:
        raise station_store.StationLeaseLostError("lease gone")

    with pytest.raises(station.StationHeldError, match="lost its lease"):
        station.run_station(
            store=store,
            station_instance=_ticking_station(tick),
            lease=lease,
            interval=0.0,
            beat_seconds=5.0,
            renew_seconds=5.0,
        )
