"""Operational readers do not open the files of ended runs."""

from __future__ import annotations

import builtins
import importlib
import io
import json
import os
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import (
    promotion,
    queue_landing,
    runs,
    station,
    station_store,
)
from reckon.crew import review as review_module
from tests import test_the_station_reads_only_what_changed as station_fixture

obligations_module = importlib.import_module("reckon.crew.obligations")
PROJECT = station_fixture.PROJECT
SESSION = station_fixture.SESSION


@pytest.fixture
def population(tmp_path, monkeypatch):
    monkeypatch.setattr(station_fixture, "LIVE_RUNS", 5)
    fleet = station_fixture.fleet.__wrapped__(tmp_path, monkeypatch)
    root = fleet["repo"]
    station_fixture._git(root, "add", "docs/plans/fixture-plan.html")
    station_fixture._git(
        root,
        "commit",
        "-q",
        "-m",
        "test: record fixture plan",
        "-m",
        "Keep the fixture plan clean for promotion checks.",
    )
    ledger.write(PROJECT, {"runs": [], "members": [], "holds": []}, 1, root)
    live = runs.list_live(project=PROJECT)
    for pointer in live:
        directory = runs.run_dir(pointer["run_id"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "worker.json").write_text(json.dumps(pointer))
        ledger.write_run_file(
            PROJECT,
            {
                "run_id": pointer["run_id"],
                "node": pointer["node"]["id"],
                "project": PROJECT,
                "session": SESSION,
                "completed_at": "2026-01-01T00:00:00Z",
            },
            root,
            fsync=False,
        )
    return fleet


def _end_history(fleet, count):
    for number in range(count):
        run_id = f"ended-{number:04d}"
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        row = {
            "run_id": run_id,
            "project": PROJECT,
            "node": run_id,
            "completed_at": "2026-01-01T00:00:00Z",
        }
        (directory / "worker.json").write_text(json.dumps(row))
        ledger.write_run_file(PROJECT, row, fleet["repo"], fsync=False)
        path = review_module.review_path(PROJECT, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        review_module._write_record(
            path,
            {
                "project": PROJECT,
                "reviewed_run_id": run_id,
                "review_run_id": f"review-{run_id}",
                "status": "parsed",
                "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
            },
        )


def _count_opens(monkeypatch):
    opened = []
    real_builtin, real_io, real_os = builtins.open, io.open, os.open

    def wrap(real):
        def counted(path, *args, **kwargs):
            if isinstance(path, (str, bytes, Path)):
                opened.append(str(path))
            return real(path, *args, **kwargs)

        return counted

    monkeypatch.setattr(builtins, "open", wrap(real_builtin))
    monkeypatch.setattr(io, "open", wrap(real_io))
    monkeypatch.setattr(os, "open", wrap(real_os))

    def history_read(*args, **kwargs):
        pytest.fail("operational read requested the complete ledger")

    monkeypatch.setattr(ledger, "load", history_read)
    return opened


def _station(fleet, tmp_path):
    store = station_store.StationStore(tmp_path / "station")
    service = station.Station(
        store,
        home=fleet["home"] / "crew",
        projects=lambda: {PROJECT: fleet["repo"] / "docs"},
        sweeps=station.SweepBeat(sweep=lambda *a, **k: {}, start=lambda callback: None),
    )
    try:
        service.tick(now=station_fixture.OBSERVED_AT)
        return sorted(row["run_id"] for row in store.pointer_rows())
    finally:
        store.close()


def _read(operation, fleet, tmp_path, monkeypatch):
    if operation == "station":
        return _station(fleet, tmp_path)
    if operation == "obligations":
        payload = obligations_module.obligations(PROJECT, SESSION)
        return sorted((row["run_id"], row["kind"]) for row in payload["obligations"])
    if operation == "landing":
        result = queue_landing.land_once()
        return {key: result.get(key) for key in ("landed", "deferred")}
    # Keep publishing and physical cleanup outside this read-population test.
    # Pointer retirement and the promotion's ledger/review readers remain real.
    pointer = runs.read_pointer("run-00")
    monkeypatch.setattr(
        promotion, "_record_landing_comment", lambda **kwargs: {"recorded": False}
    )
    monkeypatch.setattr(promotion, "_commit_landing_writes", lambda **kwargs: {})
    monkeypatch.setattr(promotion, "capture_run_session", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        promotion, "_release_after_promotion", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        promotion,
        "_record_release_on_ledger",
        lambda **kwargs: (kwargs["release"], None),
    )
    monkeypatch.setattr(promotion, "_retire_disposable_identity", lambda *args: {})
    result = promotion._complete_locked("run-00", gate="passed", root=fleet["repo"])
    assert result["already_promoted"]
    assert result["pointer_removed"]
    assert not runs.pointer_path("run-00").exists()
    runs._write_json(runs.pointer_path("run-00"), pointer)
    return {
        key: result[key]
        for key in ("run_id", "record", "already_promoted", "pointer_removed")
    }


@pytest.mark.parametrize(
    "operation", ["station", "promotion", "landing", "obligations"]
)
def test_history_does_not_change_the_open_population(
    population, tmp_path, monkeypatch, operation
):
    fleet = population
    _end_history(fleet, 20)
    with monkeypatch.context() as counting:
        opened = _count_opens(counting)
        small = _read(operation, fleet, tmp_path / "small", counting)
        small_ended = [path for path in opened if "ended-" in path]
    _end_history(fleet, 2000)
    # Positive control: this exact instrument sees a known ended-run file.
    with monkeypatch.context() as counting:
        opened = _count_opens(counting)
        (runs.run_dir("ended-0000") / "worker.json").read_text()
        assert any("ended-0000" in path for path in opened)
        opened.clear()
        large = _read(operation, fleet, tmp_path / "large", counting)
        ended = [path for path in opened if "ended-" in path]
    assert not small_ended, small_ended[:10]
    assert not ended, ended[:10]
    assert large == small
    assert len(ledger.runs(PROJECT, fleet["repo"])) == 2005


def test_ending_a_run_removes_it_from_the_live_index(population):
    root = population["repo"]
    assert "run-00" in {row["run_id"] for row in ledger.open_runs(PROJECT, root)}
    pointer = runs.read_pointer("run-00")
    assert "run-00" in {row["run_id"] for row in runs.list_live()}
    assert promotion._remove_promoted_pointer("run-00", pointer)
    assert "run-00" not in {row["run_id"] for row in runs.list_live()}
    assert "run-00" not in {row["run_id"] for row in ledger.open_runs(PROJECT, root)}


def test_cleanup_keeps_a_promoted_run_open_until_its_release(population):
    from reckon.crew import open_run_index

    root = population["repo"]
    row = {
        "run_id": "held-run",
        "node": "held",
        "project": PROJECT,
        "worktree_retention": {"worktree": str(root / "held")},
    }
    ledger.write_run_file(PROJECT, row, root)
    assert "held-run" in {
        r["run_id"] for r in open_run_index.read_rows(PROJECT, root, retained_only=True)
    }
    ledger.write_run_file(
        PROJECT, {**row, "release": {"worktree_released": True}}, root
    )
    assert "held-run" not in {
        r["run_id"] for r in open_run_index.read_rows(PROJECT, root, retained_only=True)
    }


def test_an_open_record_is_read_from_its_authoritative_document(population):
    root = population["repo"]
    source = ledger.run_path(PROJECT, "run-00", root)
    row = json.loads(source.read_text())
    source.write_text(json.dumps({**row, "outcome": "current document"}))
    assert (
        next(
            row for row in ledger.open_runs(PROJECT, root) if row["run_id"] == "run-00"
        )["outcome"]
        == "current document"
    )


def test_a_cold_profile_is_reported_without_reading_history(tmp_path, monkeypatch):
    from reckon.crew.picker import lane_context

    monkeypatch.setattr(lane_context, "_profile_stamp", lambda *args: "fixture-stamp")
    monkeypatch.setattr(
        lane_context, "_promotion_fold_inputs", lambda *args, **kwargs: None
    )

    def history(*args, **kwargs):
        pytest.fail("a promotion rebuilt the full history profile")

    monkeypatch.setattr(lane_context, "run_time_profile", history)
    from reckon.crew.open_run_index import read_scope

    with read_scope():
        result = lane_context.refresh_run_time_profile(PROJECT, appended=())
    assert result["status"] == "unavailable"
    assert "explicit history refresh" in result["reason"]


def test_operational_metadata_preserves_predecessor_ambiguity_and_session_meter(
    population,
):
    from reckon.crew.open_run_index import latest_session_row

    root = population["repo"]
    pointer = runs.read_pointer("run-00")
    base = pointer["base_sha"]
    row = {
        "run_id": "prior-node",
        "node": "another-node",
        "commits": [base],
        "session_id": "meter-session",
        "completed_at": "2026-01-02T00:00:00Z",
    }
    ledger.write_run_file(PROJECT, row, root)
    rows = ledger.open_runs(PROJECT, root)
    assert (
        ledger.predecessor_run_id(
            supplied="", base_sha=base, runs=rows, exclude_run_id="run-00"
        )
        == "prior-node"
    )
    assert latest_session_row(PROJECT, "meter-session", root)["run_id"] == "prior-node"
    ledger.write_run_file(PROJECT, {**row, "run_id": "ambiguous-node"}, root)
    assert (
        ledger.predecessor_run_id(
            supplied="",
            base_sha=base,
            runs=ledger.open_runs(PROJECT, root),
            exclude_run_id="run-00",
        )
        is None
    )
