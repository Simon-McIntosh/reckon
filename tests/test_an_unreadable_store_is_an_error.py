"""A store the ledger cannot read is a failure, not an empty project.

The store is the ledger's only store, so a read that cannot reach it must
never be indistinguishable from a project that holds no runs. Three shapes
were measured doing so: a file that is not a database, an archive that will
not attach (retired rows silently dropped while the main read carried on), and
a runs table predating the payload column (no record at all, while the id
columns still listed every id). Each is asserted here through the ledger's own
read, so the guard is shown firing where a reader meets it rather than where
the code happens to raise.

Every store resolves under a temporary config home and every ledger read under
a temporary checkout, so the real crew home is never read or written.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import ledger, run_store

PROJECT = "proj"


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store path under a temporary config home, with no state mounted."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.delenv("RECKON_MOUNTS_PATH", raising=False)
    path = tmp_path / "run_store.db"
    monkeypatch.setenv("RECKON_RUN_STORE", str(path))
    return path


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    """A checkout holding no ledger at all, so only the store can answer."""
    root = tmp_path / "checkout"
    root.mkdir()
    return root


def _record(run_id: str, day: int) -> dict[str, object]:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker-a",
        "node": f"node-{run_id}",
        "completed_at": f"2026-11-{day:02d}T00:00:00Z",
        "outcome": "completed",
        "budget": {"spent": day},
    }


def _retire_all_but(store_path: Path, keep: set[str]) -> None:
    """Retire every row but ``keep`` into the store's archive."""
    with run_store.RunStore(store_path) as sqlite_store:
        retired = sqlite_store.retire(
            before=datetime(2027, 1, 1, tzinfo=UTC),
            protected=keep,
            projects={PROJECT},
            apply=True,
        )
    assert retired["retired"], "the fixture must retire a row"


def _write_legacy_store(store_path: Path) -> None:
    """A store whose runs table predates the payload column, with one row.

    This is the shape the live store carried before the declaration moved to a
    payload blob: ten narrow columns, no schema record, and a row that only the
    id and query-key columns can name.
    """
    connection = sqlite3.connect(str(store_path))
    try:
        connection.executescript(
            """
            CREATE TABLE "runs" (
                "run_id" TEXT PRIMARY KEY,
                "project" TEXT,
                "member" TEXT,
                "node" TEXT,
                "plan" TEXT,
                "section" TEXT,
                "gate" TEXT,
                "completed_at" TEXT,
                "backend" TEXT,
                "base_sha" TEXT
            );
            CREATE TABLE "run_details" (
                "run_id" TEXT PRIMARY KEY,
                "detail" TEXT NOT NULL
            );
            CREATE TABLE "retired_runs" (
                "run_id" TEXT PRIMARY KEY,
                "project" TEXT NOT NULL
            );
            """
        )
        connection.execute(
            'INSERT INTO "runs" '
            '("run_id", "project", "member", "node", "completed_at") '
            "VALUES (?, ?, ?, ?, ?)",
            ("r-old", PROJECT, "worker-a", "node-old", "2026-10-01T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()


def test_a_damaged_store_file_raises_naming_the_store_and_the_cause(
    store: Path, checkout: Path
) -> None:
    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append(PROJECT, _record("r-1", 1))
    # The file exists and is not a database: a store that is present and
    # unreadable, which used to answer as a project with no runs.
    store.write_bytes(b"this is not a sqlite database at all\n" * 64)

    with pytest.raises(run_store.UnreadableRunStoreError) as raised:
        ledger.load(PROJECT, checkout)

    message = str(raised.value)
    assert str(store) in message
    assert "not a database" in message
    # The reader that used to break out of its batch loop and answer empty.
    with pytest.raises(run_store.UnreadableRunStoreError):
        run_store.stored_run_rows(PROJECT)


def test_an_archive_that_will_not_attach_raises_rather_than_losing_retired_rows(
    store: Path, checkout: Path
) -> None:
    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append(PROJECT, _record("r-1", 1))
        sqlite_store.append(PROJECT, _record("r-2", 2))
    _retire_all_but(store, {"r-2"})
    archive = store.with_name(f"{store.stem}.archive{store.suffix}")
    assert archive.is_file()
    archive.write_bytes(b"this archive is not a database\n" * 64)

    with pytest.raises(run_store.UnreadableRunStoreError) as raised:
        ledger.load(PROJECT, checkout)

    message = str(raised.value)
    assert str(store) in message
    assert str(archive) in message
    assert "cannot be attached" in message
    # The row that used to disappear with the failed attach.
    with pytest.raises(run_store.UnreadableRunStoreError):
        run_store.stored_run_rows(PROJECT)


def test_a_runs_table_without_a_payload_column_is_refused_naming_the_migration(
    store: Path, checkout: Path
) -> None:
    _write_legacy_store(store)

    with pytest.raises(run_store.UnreadableRunStoreError) as raised:
        ledger.load(PROJECT, checkout)

    message = str(raised.value)
    assert str(store) in message
    assert "payload" in message
    assert "migration" in message
    # The index still lists the id the unreadable reader dropped, which is the
    # disagreement that made the empty answer wrong rather than merely narrow.
    with pytest.raises(run_store.UnreadableRunStoreError):
        run_store.indexed_run_ids(PROJECT)


def test_a_store_that_owes_its_migration_is_migrated_before_it_is_read(
    store: Path, checkout: Path
) -> None:
    _write_legacy_store(store)

    # Opening through RunStore is a write, and the write migrates the store.
    with run_store.RunStore(store) as sqlite_store:
        assert sqlite_store.get_run("r-old") is not None

    rows = run_store.stored_run_rows(PROJECT)
    assert [str(record["run_id"]) for record in rows or []] == ["r-old"]
    data, _version = ledger.load(PROJECT, checkout)
    assert [str(rows["run_id"]) for rows in data["runs"]] == ["r-old"]


def test_an_absent_store_is_still_an_empty_answer(store: Path, checkout: Path) -> None:
    assert not store.exists()
    assert run_store.stored_run_rows(PROJECT) is None
    assert run_store.indexed_run_ids(PROJECT) is None
    data, _version = ledger.load(PROJECT, checkout)
    assert data["runs"] == []


def test_the_ledger_reader_and_the_indexed_reader_return_the_same_rows(
    store: Path,
) -> None:
    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append(PROJECT, _record("r-1", 1))
        sqlite_store.append(PROJECT, _record("r-2", 2))
    _retire_all_but(store, {"r-2"})
    with run_store.RunStore(store) as sqlite_store:
        indexed = sqlite_store._indexed_records(include_archive=True)

    ledger_rows = {
        str(record["run_id"]): record
        for record in run_store.stored_run_rows(PROJECT) or []
    }
    # Non-empty, and holding the retired row as well as the hot one: an empty
    # store would satisfy a comparison of two empty answers.
    assert sorted(ledger_rows) == ["r-1", "r-2"]
    assert ledger_rows == indexed
