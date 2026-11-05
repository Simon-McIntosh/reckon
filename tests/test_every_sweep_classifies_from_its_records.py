"""Each sweep supplies its own live population when classifying a scoring run."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

import pytest

from reckon import roadmap_snapshot
from reckon.crew import (
    recovery_review_dispatch,
    recovery_watch,
    runs,
    station_store,
)


@pytest.fixture
def scoring_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    recovery_watch._SNAPSHOT_CACHE.clear()
    run_id = "r-finished"
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: {run_id}\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "node": {"id": run_id, "plan": "fixture", "section": "section"},
        "backend": "local",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
        "created_at": "2026-11-05T10:00:00Z",
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return home, record


@pytest.fixture
def live_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    reads: list[int] = []
    real = recovery_review_dispatch.list_live
    sweep_thread = threading.get_ident()

    def counting(*args, **kwargs):
        # A watch transition can start an obligations sweep on another thread.
        # Count the entry point under test, not that independent sweep.
        if threading.get_ident() == sweep_thread:
            reads.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(recovery_review_dispatch, "list_live", counting)
    return reads


def _scoring(snapshot: dict, reads: list[int]) -> None:
    assert snapshot["classification"] == "scoring"
    assert reads == [], f"scoring row listed the live set {len(reads)} time(s)"


def test_publisher_snapshot_sweep_uses_its_iterable_once(scoring_run, live_reads):
    _home, record = scoring_run
    seen: list[str] = []

    def records():
        seen.append("read")
        yield record

    snapshots = runs._watch_stream_snapshots(records(), stall_window="10m")
    _scoring(snapshots[record["run_id"]], live_reads)
    assert seen == ["read"]


def test_roadmap_snapshot_sweep_uses_its_population(scoring_run, live_reads):
    _home, record = scoring_run
    rows, states = roadmap_snapshot._roadmap_live_readings("sample")
    assert rows
    _scoring(states[record["run_id"]], live_reads)


def test_station_tick_uses_its_parsed_population(scoring_run, live_reads, tmp_path):
    _home, record = scoring_run
    store = station_store.StationStore(tmp_path / "station")
    store.tick(home=runs.crew_home())
    verdicts = store.classify_tick()
    _scoring(verdicts[record["run_id"]], live_reads)


def test_quiet_beat_uses_its_population(scoring_run, live_reads):
    _home, record = scoring_run
    snapshots = recovery_watch._quiet_beat_snapshots(
        [record], {}, moment=1_900_000_000.0, stall_seconds=600
    )
    _scoring(snapshots[record["run_id"]], live_reads)


def test_session_counts_use_their_population(scoring_run, live_reads, monkeypatch):
    _home, _record = scoring_run
    classifications: list[str] = []
    real_snapshot = recovery_watch._watch_snapshot

    def observed_snapshot(*args, **kwargs):
        snapshot = real_snapshot(*args, **kwargs)
        classifications.append(snapshot["classification"])
        return snapshot

    monkeypatch.setattr(recovery_watch, "_watch_snapshot", observed_snapshot)
    counts = recovery_watch._live_project_session_counts(
        "sample", "session-orchestrating"
    )
    assert counts
    assert classifications == ["scoring"]
    assert live_reads == []


class _StopTickerError(Exception):
    pass


def test_watch_ticker_snapshot_pass_uses_its_population(
    scoring_run, live_reads, monkeypatch: pytest.MonkeyPatch
):
    _home, record = scoring_run
    classified: list[str] = []
    real_snapshot = recovery_watch._watch_snapshot

    def measured_snapshot(*args, **kwargs):
        before = len(live_reads)
        snapshot = real_snapshot(*args, **kwargs)
        if snapshot["classification"] == "scoring":
            classified.append(snapshot["classification"])
            assert len(live_reads) == before
        return snapshot

    monkeypatch.setattr(recovery_watch, "_watch_snapshot", measured_snapshot)

    def stop(_interval):
        raise _StopTickerError

    with contextlib.suppress(_StopTickerError):
        list(recovery_watch.watch_ticker("sample", poll_interval=0, sleeper=stop))
    assert classified, f"ticker never classified {record['run_id']} as scoring"
