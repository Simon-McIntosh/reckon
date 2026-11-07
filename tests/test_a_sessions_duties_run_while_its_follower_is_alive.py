"""A changed session gets another sweep when its current sweep is still running."""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import resumption, station, station_store

PROJECT = "session-duties-fixture"
SESSION = "coordinator-session"
MOMENT = datetime(2026, 11, 7, 18, 10, tzinfo=UTC)


def _pointer(home: Path, run_id: str) -> Path:
    stream = home / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"system","subtype":"init"}\n', encoding="utf-8")
    path = home / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "session": SESSION,
                "phase": "working",
                "launcher_host": socket.gethostname(),
                "pid": os.getpid(),
                "created_at": "2026-11-07T18:00:00Z",
                "log_path": str(stream),
                "worktree": str(home / "trees" / run_id),
                "node": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _deliver(path: Path, *, blocking_review: bool = False) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    record["phase"] = "complete"
    if blocking_review:
        record["stored_review"] = {"findings": [{"severity": "blocking"}]}
    else:
        record["review_owed"] = True
    path.write_text(json.dumps(record), encoding="utf-8")


def test_changed_session_runs_its_review_and_repair_on_the_next_requested_sweep(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_run = _pointer(home, "r-needs-review")
    repair_run = _pointer(home, "r-needs-repair")
    entered = threading.Event()
    release = threading.Event()
    observed: list[tuple[str, str]] = []
    calls: list[tuple[str, str]] = []

    def review_pass(**_kwargs: object) -> dict[str, list[str]]:
        dispatched: list[str] = []
        repaired: list[str] = []
        for record in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in (review_run, repair_run)
        ):
            if record.get("review_owed"):
                dispatched.append(record["run_id"])
                observed.append(("review-dispatched", record["run_id"]))
            if any(
                finding.get("severity") == "blocking"
                for finding in record.get("stored_review", {}).get("findings", [])
            ):
                repaired.append(record["run_id"])
                observed.append(("repair-opened", record["run_id"]))
        return {"dispatched": dispatched, "repaired": repaired}

    def slow_plan_pass(*_args: object, **_kwargs: object) -> dict:
        # The first sweep has finished its review and repair pass, then waits
        # in later work while the two source runs are delivered.
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=10), "the controlled sweep was not released"
        return {}

    monkeypatch.setattr(resumption, "dispatch_awaiting_reviews", review_pass)
    monkeypatch.setattr(
        resumption.plan_review, "compose_owed_plan_reviews", slow_plan_pass
    )
    monkeypatch.setattr(resumption, "external_wait", lambda *a, **k: None)
    monkeypatch.setattr(resumption, "_stream_refusal_block", lambda *a, **k: None)

    def sweep(project: str, *, session: str, **kwargs: object) -> dict:
        calls.append((project, session))
        return resumption.sweep(project, session=session, **kwargs)

    threads: list[threading.Thread] = []

    def start(run) -> None:
        thread = threading.Thread(target=run)
        threads.append(thread)
        thread.start()

    store = station_store.StationStore(tmp_path / "station")
    beat = station.SweepBeat(sweep=sweep, start=start)
    node = station.Station(store, home=home, projects=dict, sweeps=beat)
    node.tick(now=MOMENT)
    assert entered.wait(timeout=10), "the station never requested the first sweep"

    try:
        _deliver(review_run)
        _deliver(repair_run, blocking_review=True)
        node.tick(now=MOMENT + timedelta(seconds=station.SWEEP_BEAT_SECONDS + 1))
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert calls == [(PROJECT, SESSION), (PROJECT, SESSION)]
    assert ("review-dispatched", "r-needs-review") in observed
    assert ("repair-opened", "r-needs-repair") in observed
    reports = [
        json.loads(line)
        for line in (home / "crew" / "recovery" / f"{PROJECT}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert reports[-1]["reviews"]["dispatched"] == ["r-needs-review"]
    assert reports[-1]["reviews"]["repaired"] == ["r-needs-repair"]
