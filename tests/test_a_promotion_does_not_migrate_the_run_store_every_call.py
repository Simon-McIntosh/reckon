"""The run store is migrated once per home, not once per promotion.

Every promotion opens the run store to append its row. Opening a store whose
runs table still carries an earlier shape used to backfill every row each
time — decode the payload, rewrite the row — because the legacy narrow
columns stay on the table as harmless extras, so the shape never matches the
current declaration and the pass never stops being owed.

A store now records the declaration version it was migrated to, and an open
that finds that record skips the pass. These tests count the passes a
promotion's own append takes, through a wrapper on the migration itself, so
the measure is the work the store does rather than a claim about it. Every
store resolves under a temporary config home, leaving the real crew home
untouched.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from reckon import ledger, run_store

# The runs table as the original ten-column declaration left it. The live
# store carries its rows in this shape, and it is the shape that kept the
# backfill owed on every open: the migration adds the query keys and the
# payload beside these columns rather than replacing them.
_LEGACY_RUNS = """
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
CREATE TABLE "run_details" ("run_id" TEXT PRIMARY KEY, "detail" TEXT NOT NULL);
CREATE TABLE "members" ("member_id" TEXT PRIMARY KEY, "payload" TEXT NOT NULL);
CREATE TABLE "holds" ("hold_id" TEXT PRIMARY KEY, "payload" TEXT NOT NULL);
"""


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary config home: its store, its state and its mounts."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_RUN_STORE", raising=False)
    return tmp_path


@pytest.fixture()
def migration_passes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the run-store migration passes a promotion path takes.

    The wrapper sits on the migration step itself, so a zero is the store
    saying it did not run rather than a call counted one level too high. The
    counters are reset by the tests that need a second phase, and one test
    always observes a non-zero count first, so a wrapper that failed to
    install would be visible as a zero where a pass is owed.
    """
    counted = {"passes": 0}
    original = run_store.RunStore._migrate_runs

    def counting(self: run_store.RunStore) -> None:
        counted["passes"] += 1
        original(self)

    monkeypatch.setattr(run_store.RunStore, "_migrate_runs", counting)
    return counted


def _promote(home: Path, run_id: str) -> dict:
    """Promote one run through the ledger append a promotion actually calls.

    ``ledger.append_run`` writes the run file and then updates the run store,
    which is the path whose store cost these tests measure. The ledger is
    created in the temporary checkout, so nothing reaches a real project.
    """
    return ledger.append_run(
        "proj",
        {
            "run_id": run_id,
            "member": "worker-a",
            "node": f"node-{run_id}",
            "gate": "passed",
            "completed_at": "2026-11-11T00:00:00Z",
        },
        root=home / "checkout",
        allow_create=True,
    )


def _store_path() -> Path:
    return run_store.store_path()


def _recorded_version(path: Path) -> str | None:
    with sqlite3.connect(str(path)) as connection:
        row = connection.execute(
            'SELECT "value" FROM "store_meta" WHERE "key" = ?',
            (run_store._SCHEMA_RECORD_KEY,),
        ).fetchone()
    return None if row is None else str(row[0])


def test_a_promotion_on_a_migrated_home_runs_no_migration_step(
    home: Path, migration_passes: dict[str, int]
) -> None:
    # The first promotion builds and migrates the store on this home.
    first = _promote(home, "r-first")
    assert first["store"] == {"status": "written"}
    assert migration_passes["passes"] == 1
    store_path = _store_path()
    assert _recorded_version(store_path) == run_store._STORE_SCHEMA_VERSION

    # The home is now already migrated: a later promotion only appends.
    migration_passes["passes"] = 0
    second = _promote(home, "r-second")
    assert second["store"] == {"status": "written"}
    assert migration_passes["passes"] == 0

    # The skip is of a store that is genuinely current, not of a broken one:
    # both runs answer through the ordinary reader.
    with run_store.RunStore() as store:
        assert store.get_run("r-first")["run_id"] == "r-first"
        assert store.get_run("r-second")["run_id"] == "r-second"


def test_a_home_without_the_record_migrates_once(
    home: Path, migration_passes: dict[str, int]
) -> None:
    # Nothing has created a store on this home, so it carries no record and
    # the first promotion owes the pass exactly once.
    assert not _store_path().exists()
    _promote(home, "r-first")
    assert migration_passes["passes"] == 1

    migration_passes["passes"] = 0
    _promote(home, "r-second")
    assert migration_passes["passes"] == 0


def test_a_store_from_the_legacy_shape_migrates_once_and_then_not(
    home: Path, migration_passes: dict[str, int]
) -> None:
    # A store carrying the earlier narrow columns has no record, so the first
    # promotion migrates it, backfilling the rows it already holds.
    store_path = _store_path()
    store_path.parent.mkdir(parents=True)
    with sqlite3.connect(str(store_path)) as connection:
        connection.executescript(_LEGACY_RUNS)
        for index in range(3):
            connection.execute(
                'INSERT INTO "runs" ("run_id", "project", "member", "node") '
                "VALUES (?, ?, ?, ?)",
                (f"r-old-{index}", "proj", "worker-a", f"node-{index}"),
            )
        connection.commit()

    _promote(home, "r-first")
    assert migration_passes["passes"] == 1

    # The legacy columns stay on the table, so the shape still differs from
    # the current declaration; the record is what stops the pass repeating.
    with sqlite3.connect(str(store_path)) as connection:
        columns = {
            str(row[1]) for row in connection.execute('PRAGMA table_info("runs")')
        }
        stored = connection.execute(
            'SELECT "payload" FROM "runs" WHERE "run_id" = ?', ("r-old-0",)
        ).fetchone()
    assert {"run_id", "payload", "base_sha"} <= columns
    assert json.loads(stored[0])["run_id"] == "r-old-0"

    migration_passes["passes"] = 0
    _promote(home, "r-second")
    assert migration_passes["passes"] == 0

    with run_store.RunStore() as store:
        assert store.get_run("r-old-0")["member"] == "worker-a"
        assert store.get_run("r-second")["run_id"] == "r-second"


def test_a_changed_declaration_version_is_not_taken_as_current(home: Path) -> None:
    # The record names the declaration the store was migrated to, so a record
    # left by different code is not accepted: opening the store again after
    # the recorded value moves must migrate rather than skip.
    _promote(home, "r-first")
    store_path = _store_path()
    with sqlite3.connect(str(store_path)) as connection:
        connection.execute(
            'UPDATE "store_meta" SET "value" = ? WHERE "key" = ?',
            ("an-earlier-declaration", run_store._SCHEMA_RECORD_KEY),
        )
        connection.commit()

    passes = {"n": 0}
    original = run_store.RunStore._migrate_runs

    def counting(self: run_store.RunStore) -> None:
        passes["n"] += 1
        original(self)

    run_store.RunStore._migrate_runs = counting  # type: ignore[assignment]
    try:
        run_store.RunStore().close()
    finally:
        run_store.RunStore._migrate_runs = original  # type: ignore[assignment]

    assert passes["n"] == 1
    assert _recorded_version(store_path) == run_store._STORE_SCHEMA_VERSION
