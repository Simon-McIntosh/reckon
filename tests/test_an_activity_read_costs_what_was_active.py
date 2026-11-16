"""Recent activity reads visit recent runs rather than their entire history."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime

import pytest

from reckon import ledger, run_store

BOUNDARY = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
STAMPS = ("launched_at", "created_at", "dispatched_at", "completed_at")


@pytest.fixture(scope="module")
def population(tmp_path_factory):
    path = tmp_path_factory.mktemp("activity-store") / "runs.db"
    records = []
    with run_store.RunStore(path) as store, store._conn:
        for index in range(20_000):
            record = {
                "run_id": f"run-{index:05d}",
                "project": "example",
                "plan": f"topic-{index % 7}",
                "outcome": f"payload-{index}",
                "launched_at": "2020-01-01T00:00:00Z",
                "created_at": "malformed",
                "dispatched_at": None,
                "completed_at": "2020-01-02T00:00:00Z",
            }
            if index % 400 == 0:
                # All four fields independently qualify, with exact-boundary,
                # fractional-second and offset spellings of the same instant.
                record[STAMPS[(index // 400) % len(STAMPS)]] = (
                    "2025-01-01T01:00:00+01:00"
                    if index % 800 == 0
                    else "2025-01-01T00:00:00.000001Z"
                )
            records.append(record)
            store._conn.execute(
                run_store._RUNS_INSERT,
                (
                    record["run_id"],
                    *(record.get(key) for key in run_store.QUERY_KEYS),
                    None,
                    json.dumps(record),
                ),
            )
        assert store._conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 20_000
    return path, records


def _legacy_copy(source, target, *, omit_revision=False):
    """Copy payloads into the schema without a cached activity instant."""
    with sqlite3.connect(source) as original, sqlite3.connect(target) as legacy:
        columns = ["run_id", *run_store.QUERY_KEYS, "payload"]
        if not omit_revision:
            columns.append("revision")
        declarations = ", ".join(
            f'"{key}" TEXT' + (" PRIMARY KEY" if key == "run_id" else "")
            for key in columns
        )
        legacy.execute(f"CREATE TABLE runs ({declarations})")
        names = ", ".join(f'"{key}"' for key in columns)
        legacy.executemany(
            f"INSERT INTO runs ({names}) VALUES ({','.join('?' for _ in columns)})",  # noqa: S608 - fixed schema columns
            original.execute(f"SELECT {names} FROM runs"),  # noqa: S608 - fixed schema columns
        )


def test_an_interrupted_backfill_retries_the_unfinished_rows(
    tmp_path, monkeypatch, population
):
    source, _ = population
    path = tmp_path / "interrupted.db"
    _legacy_copy(source, path, omit_revision=True)
    original = run_store._legacy_payload
    calls = []

    def interrupted(row):
        calls.append(row["run_id"])
        if len(calls) == 2:
            raise RuntimeError("injected backfill interruption")
        return original(row)

    monkeypatch.setattr(run_store, "_legacy_payload", interrupted)
    with pytest.raises(RuntimeError, match="injected backfill interruption"):
        run_store.RunStore(path)
    assert len(calls) == 2
    calls.clear()

    def counted(row):
        calls.append(row["run_id"])
        return original(row)

    monkeypatch.setattr(run_store, "_legacy_payload", counted)
    with run_store.RunStore(path):
        pass
    assert len(calls) == 20_000, "an unfinished backfill must run again"
    assert (
        len(run_store.stored_run_rows("example", path=path, activity_since=BOUNDARY))
        == 50
    )


@pytest.mark.parametrize("storage", ["fresh", "migrated", "archive", "amended"])
def test_an_activity_read_visits_only_active_rows(
    tmp_path, monkeypatch, population, storage
):
    source, records = population
    path = tmp_path / "runs.db"
    expected = [row for row in records if ledger.run_activity_seconds(row) >= BOUNDARY]
    assert len(expected) == 50
    assert {STAMPS[(index // 400) % 4] for index in range(0, 20_000, 400)} == set(
        STAMPS
    )
    if storage == "migrated":
        _legacy_copy(source, path)
        with run_store.RunStore(path):
            pass
    elif storage == "archive":
        _legacy_copy(source, run_store._archive_path(path))
        with run_store.RunStore(path) as store:
            store._attach_archive(create=True)
            with store._conn:
                store._conn.executemany(
                    "INSERT INTO retired_runs (run_id, project) VALUES (?, ?)",
                    ((row["run_id"], "example") for row in records),
                )
    else:
        shutil.copyfile(source, path)
    if storage in ("amended", "archive"):
        with run_store.RunStore(path) as store:
            stale = {**expected.pop(0), "launched_at": "malformed"}
            assert store.sync_run("example", stale) == "updated"
            recent = {**records[1], "completed_at": "2025-01-01T00:00:00Z"}
            assert store.sync_run("example", recent) == "updated"
            expected.append(recent)
            if storage == "amended":
                appended = {**expected[-1], "run_id": "appended"}
                store.append("example", appended)
                inserted = {**appended, "run_id": "inserted"}
                assert store.sync_run("example", inserted) == "inserted"
                expected.extend((appended, inserted))

    monkeypatch.setenv("RECKON_RUN_STORE", str(path))
    monkeypatch.setattr(ledger, "_aggregate_headers", lambda *_: ({"runs": []}, 0))
    original_connect = sqlite3.connect
    measured = {"rows": 0, "steps": 0}

    def count_row(_cursor, row):
        measured["rows"] += 1
        return row

    def progress():
        measured["steps"] += 100
        return 0

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.row_factory = count_row
        connection.set_progress_handler(progress, 100)
        return connection

    monkeypatch.setattr(run_store.sqlite3, "connect", connect)
    # A positive control proves both instruments see the history when read.
    with connect(path) as connection:
        assert len(connection.execute("SELECT run_id FROM runs").fetchall()) == (
            0 if storage == "archive" else 20_000 + 2 * (storage == "amended")
        )
        if storage == "archive":
            connection.execute(
                "ATTACH DATABASE ? AS history", (str(run_store._archive_path(path)),)
            )
            assert (
                len(connection.execute("SELECT run_id FROM history.runs").fetchall())
                == 20_000
            )
    assert measured["rows"] >= 20_000
    assert measured["steps"] >= 20_000
    measured.update(rows=0, steps=0)

    actual, _ = ledger.read_records(
        "example", with_figures=False, activity_since=BOUNDARY
    )
    assert {row["run_id"]: row for row in actual} == {
        row["run_id"]: row for row in expected
    }
    print(
        f"{storage}: history=20000 active={len(expected)} rows={measured['rows']} steps={measured['steps']}"
    )
    assert measured["rows"] <= len(expected) * 3, measured
    assert measured["steps"] <= len(expected) * 200, measured
