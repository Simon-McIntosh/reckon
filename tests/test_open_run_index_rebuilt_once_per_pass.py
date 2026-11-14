"""An operational pass reuses one complete open-run index."""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import sqlite3
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from reckon import ledger
from reckon.crew import open_run_index, runs, station, station_store

PROJECT = "sample"
POPULATION = 50
MOMENT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "crew"
    monkeypatch.setenv("RECKON_HOME", str(home))
    rows = [
        {"run_id": f"terminal-{index}", "node": f"node-{index}"}
        for index in range(POPULATION)
    ]
    aggregate = ledger.ledger_path(PROJECT, repo)
    aggregate.parent.mkdir(parents=True)
    (aggregate.parent / "runs").mkdir()
    aggregate.write_text(
        json.dumps({"version": 1, "data": {"members": [], "holds": [], "runs": rows}})
    )
    ledger._bump_generation(PROJECT)
    assert open_run_index.has_row(PROJECT, rows[0]["run_id"], repo)
    assert not any(
        ledger.run_path(PROJECT, row["run_id"], repo).exists() for row in rows
    )
    assert not open_run_index.has_row(PROJECT, "absent", repo)
    return repo, home, rows


def _stale(repo):
    # Hold projection publication to expose the gap after the authoritative write.
    with patch.object(open_run_index, "record_written", lambda *args, **kwargs: None):
        ledger.write_run_file(
            PROJECT,
            {"run_id": "concurrent-writer", "node": "writer"},
            repo,
            fsync=False,
        )


def _count_rebuilds(monkeypatch):
    loads = []
    original = ledger.load

    def load(*args, **kwargs):
        loads.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "load", load)
    return loads


def _tick(indexed, monkeypatch, tmp_path, lookup):
    repo, home, rows = indexed
    pointers = [
        {**row, "project": PROJECT, "phase": "completed", "session": "fixture-session"}
        for row in rows
    ]
    monkeypatch.setattr(runs, "list_live", lambda: pointers)
    store = station_store.StationStore(tmp_path / "station")
    monkeypatch.setattr(store, "tick", lambda **kwargs: None)
    monkeypatch.setattr(
        store,
        "classify_tick",
        lambda **kwargs: {row["run_id"]: row for row in pointers},
    )
    monkeypatch.setattr(
        store,
        "pointer_rows",
        lambda: [
            {"run_id": row["run_id"], "fields": json.dumps(row)} for row in pointers
        ],
    )
    monkeypatch.setattr(
        station.recovery,
        "fleet_transitions",
        lambda known, current, **kwargs: ([], current),
    )
    monkeypatch.setattr(
        station.recovery_watch, "handover_run_ids", lambda project: set()
    )

    def publish(*args, **kwargs):
        for index, row in enumerate(rows):
            lookup(index, row["run_id"])

    monkeypatch.setattr(station.station_obligations, "publish", publish)
    node = station.Station(
        store,
        home=home,
        projects=lambda: {PROJECT: repo / "docs"},
        duties=SimpleNamespace(
            committing=contextlib.nullcontext, request=lambda *args, **kwargs: None
        ),
        sweeps=SimpleNamespace(request=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(node, "_due_reset_events", lambda *args: ([], set()))
    try:
        node.tick(now=MOMENT)
    finally:
        store.close()


def test_one_station_tick_rebuilds_at_most_once(indexed, monkeypatch, tmp_path, capsys):
    repo, _home, _rows = indexed
    loads = _count_rebuilds(monkeypatch)
    _stale(repo)
    answered = []

    def lookup(index, run_id):
        answered.append(open_run_index.has_row(PROJECT, run_id, repo))
        if index == 0:
            _stale(repo)

    _tick(indexed, monkeypatch, tmp_path, lookup)
    with capsys.disabled():
        print(f"tick: lookups={len(answered)} rebuilds={len(loads)}")
    assert len(answered) == POPULATION and all(answered)
    assert len(loads) == 1, f"one tick rebuilt {len(loads)} times"


def test_rebuilt_index_answers_later_stale_lookup_without_writing(
    indexed, monkeypatch, tmp_path, capsys
):
    repo, _home, _rows = indexed
    loads = _count_rebuilds(monkeypatch)
    _stale(repo)
    writes = []
    real_connect = sqlite3.connect
    path = open_run_index.index_path(PROJECT, repo)

    def connect(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        if str(path) in str(database):
            connection.set_trace_callback(
                lambda sql: (
                    writes.append(sql)
                    if sql.split()[0].upper()
                    in {"BEGIN", "CREATE", "INSERT", "UPDATE", "DELETE", "REPLACE"}
                    else None
                )
            )
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    observations = []

    def lookup(index, run_id):
        if index == 0:
            assert open_run_index.has_row(PROJECT, run_id, repo)
            _stale(repo)
            writes.clear()
        else:
            with open_run_index.read_scope():
                assert open_run_index.has_row(PROJECT, run_id, repo)
            observations.append((len(loads), list(writes)))

    _tick(indexed, monkeypatch, tmp_path, lookup)
    assert len(observations) == POPULATION - 1
    assert observations == [(1, [])] * (POPULATION - 1)
    with capsys.disabled():
        print(
            f"after rebuild: lookups={len(observations)} rebuilds=0 write_statements={len(writes)}"
        )


def test_current_lookup_does_not_open_a_writer(indexed, monkeypatch):
    repo, _home, rows = indexed

    def refuse(*args, **kwargs):
        pytest.fail("a current index lookup opened a writer")

    monkeypatch.setattr(open_run_index, "_connect", refuse)
    assert open_run_index.has_row(PROJECT, rows[0]["run_id"], repo)


def _hold_rebuild(repo, held, release):
    original = ledger.load

    def load(*args, **kwargs):
        held.set()
        if not release.wait(4):
            raise RuntimeError("rebuild release was not received")
        return original(*args, **kwargs)

    ledger.load = load
    open_run_index._ensure(PROJECT, repo)


@pytest.mark.timeout(15)
def test_lookup_reads_last_complete_index_while_another_process_rebuilds(
    indexed, capsys, monkeypatch
):
    repo, _home, rows = indexed
    _stale(repo)
    context = multiprocessing.get_context("fork")
    held, release = context.Event(), context.Event()
    process = context.Process(target=_hold_rebuild, args=(repo, held, release))
    process.start()
    try:
        assert held.wait(3), "the second process never entered its rebuild"

        def refuse_history(*args, **kwargs):
            pytest.fail("a concurrent lookup escaped to historical ledger reads")

        for name in ("load", "runs", "load_aggregate"):
            monkeypatch.setattr(ledger, name, refuse_history)
        started = time.monotonic()
        assert open_run_index.has_row(PROJECT, rows[0]["run_id"], repo)
        assert not open_run_index.has_row(PROJECT, "absent", repo)
        elapsed = time.monotonic() - started
        assert process.is_alive(), (
            "the lookup waited until the rebuilding process ended"
        )
        assert elapsed < 1, f"lookup waited {elapsed:.3f}s behind the rebuild"
        with capsys.disabled():
            print(f"concurrent rebuild: lookups=2 elapsed_seconds={elapsed:.3f}")
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
    assert process.exitcode == 0


def test_next_pass_revalidates_the_ledger(indexed, monkeypatch):
    repo, _home, rows = indexed
    loads = _count_rebuilds(monkeypatch)
    monkeypatch.setattr(runs, "list_live", list)
    with open_run_index.read_scope():
        assert open_run_index.has_row(PROJECT, rows[0]["run_id"], repo)
        _stale(repo)
        assert open_run_index.has_row(PROJECT, rows[0]["run_id"], repo)
        assert loads == []
    with open_run_index.read_scope():
        assert open_run_index.has_row(PROJECT, rows[0]["run_id"], repo)
    assert len(loads) == 1


@pytest.mark.parametrize("entrypoint", ["admission", "review"])
def test_operational_entrypoints_share_validation(indexed, monkeypatch, entrypoint):
    from reckon.crew import recovery_review_dispatch
    from reckon.crew.admission import selection

    repo, _home, rows = indexed
    monkeypatch.setattr(runs, "list_live", list)
    loads = _count_rebuilds(monkeypatch)
    _stale(repo)

    class ReachedLookupError(Exception):
        pass

    def lookup(*args, **kwargs):
        assert open_run_index.has_row(PROJECT, rows[0]["run_id"], repo)
        _stale(repo)
        assert open_run_index.has_row(PROJECT, rows[1]["run_id"], repo)
        raise ReachedLookupError

    if entrypoint == "admission":
        monkeypatch.setattr(selection, "_dispatch_fleet_gate", lookup)
        action = selection.admit_once
    else:
        monkeypatch.setattr(
            recovery_review_dispatch, "_settled_delivery_refusal", lookup
        )

        def action():
            return recovery_review_dispatch.dispatch_review_for_run({"subject": "plan"})

    with pytest.raises(ReachedLookupError):
        action()
    assert len(loads) == 1
