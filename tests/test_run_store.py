"""The embedded run store: schema from one declaration, single-row appends.

The store is a shadow beside the committed ledger file: nothing reads it yet,
and nothing in this module reads it either — only the append a promotion makes,
the schema it creates on first use, and the path resolver the isolation
assertions use. These tests pin the expand-stage contract: the runs table is
split into a narrow durable row and a wide detail row keyed to it, the durable
columns come from the single ``DURABLE_FIELDS`` declaration, an append inserts
one durable row and its detail in one transaction without touching any other
row, and every test's store resolves to a temporary directory leaving the real
crew config home untouched.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from reckon import run_store


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    """A temporary SQLite path under the test's own temp home."""
    return tmp_path / "run_store.db"


def _record(run_id: str, **overrides: object) -> dict:
    record = {
        "run_id": run_id,
        "member": "worker-a",
        "node": "node-a",
        "plan": "plan-a",
        "section": "s3",
        "gate": "passed",
        "completed_at": "2026-09-09T00:00:00Z",
        "backend": "clive",
        "base_sha": "aaaa1111",
        "outcome": "wide detail that does not belong in the durable row",
        "commits": ["abc123"],
    }
    record.update(overrides)
    return record


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _real_config_home() -> Path:
    xdg = Path.home() / ".config" / "reckon"
    return xdg if xdg.exists() else Path.home() / "docs-server"


def _real_store_path() -> Path:
    return _real_config_home() / "crew" / "run_store.db"


# ── The store path and write isolation ─────────────────────────────────────


def test_store_path_resolves_under_the_test_config_home(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _real_config_home()
    monkeypatch.setenv("RECKON_HOME", str(home.parent / "isolated"))
    resolved = run_store.store_path()
    assert str(resolved).startswith(str(home.parent / "isolated" / "crew"))
    # The real crew config home is not the home this test resolves to.
    assert not str(resolved).startswith(str(home))


def test_store_is_created_on_first_use_with_separate_tables(
    store: Path,
) -> None:
    real_store = _real_store_path()
    was_present = real_store.exists()

    # First use through the default seam creates the store under the
    # configured (temporary) config home.
    run_store.append("proj", _record("r-one"))

    store_location = run_store.store_path()
    assert store_location.is_file()
    with _connect(store_location) as connection:
        tables = _table_names(connection)
        assert {"runs", "run_details", "members", "holds"} <= tables
        # The run row is split into a narrow durable row and a wide detail row.
        durable_columns = {
            str(row["name"]) for row in connection.execute('PRAGMA table_info("runs")')
        }
        detail_columns = {
            str(row["name"])
            for row in connection.execute('PRAGMA table_info("run_details")')
        }
        assert "run_id" in durable_columns
        assert {"run_id", "detail"} <= detail_columns

    # The real crew config home was not written to.
    assert real_store.exists() == was_present
    # The write landed under the temporary config home the suite provides.
    configured_home = os.environ.get("RECKON_HOME", "")
    assert configured_home and str(store_location).startswith(configured_home)


# ── The durable columns come from one declaration ──────────────────────────


def test_durable_columns_match_the_single_declaration(
    store: Path,
) -> None:
    with run_store.RunStore(store):
        pass
    with _connect(store) as connection:
        columns = [
            str(row["name"]) for row in connection.execute('PRAGMA table_info("runs")')
        ]
        assert columns == [name for name, _sql_type in run_store.DURABLE_FIELDS]


def test_adding_a_durable_field_is_one_declaration_change(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        run_store,
        "DURABLE_FIELDS",
        (*run_store.DURABLE_FIELDS, ("extra", "TEXT")),
    )

    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append("proj", _record("r-extra", extra="declared"))
        with _connect(store) as connection:
            columns = {
                str(row["name"])
                for row in connection.execute('PRAGMA table_info("runs")')
            }
            assert "extra" in columns
            value = connection.execute(
                'SELECT "extra" FROM "runs" WHERE "run_id" = ?', ("r-extra",)
            ).fetchone()
            assert value[0] == "declared"


# ── One transaction, and no other row is read or rewritten ─────────────────


def test_append_writes_one_durable_and_one_detail_row_and_leaves_others_alone(
    store: Path, tmp_path: Path
) -> None:
    real_store = _real_store_path()
    was_present = real_store.exists()

    with run_store.RunStore(store) as sqlite_store:
        for suffix in ("one", "two", "three"):
            sqlite_store.append("proj", _record(f"r-{suffix}"))

    with _connect(store) as connection:
        before = {
            str(row["run_id"]): str(row["detail"])
            for row in connection.execute(
                'SELECT "run_id", "detail" FROM "run_details" ORDER BY "run_id"'
            )
        }
        durable_before = {
            str(row["run_id"]): (
                str(row["member"]),
                str(row["node"]),
                str(row["gate"]),
                str(row["completed_at"]),
            )
            for row in connection.execute(
                'SELECT "run_id", "member", "node", "gate", "completed_at" '
                'FROM "runs" ORDER BY "run_id"'
            )
        }

    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append("proj", _record("r-four"))

    with _connect(store) as connection:
        after = {
            str(row["run_id"]): str(row["detail"])
            for row in connection.execute(
                'SELECT "run_id", "detail" FROM "run_details" ORDER BY "run_id"'
            )
        }
        durable_after = {
            str(row["run_id"]): (
                str(row["member"]),
                str(row["node"]),
                str(row["gate"]),
                str(row["completed_at"]),
            )
            for row in connection.execute(
                'SELECT "run_id", "member", "node", "gate", "completed_at" '
                'FROM "runs" ORDER BY "run_id"'
            )
        }

    # Every pre-existing row is byte-identical after the append.
    assert {run_id: after[run_id] for run_id in before} == before
    assert {
        run_id: durable_after[run_id] for run_id in durable_before
    } == durable_before
    # The new row landed in both halves.
    assert "r-four" in after
    assert "r-four" in durable_after
    assert "r-four" not in before

    assert real_store.exists() == was_present


def test_a_failed_detail_write_rolls_back_the_whole_append(
    store: Path,
) -> None:
    with run_store.RunStore(store) as sqlite_store:
        # The durable half inserts fine; the detail half cannot serialise the
        # record, so the single transaction must roll both back.
        broken = _record("r-broken")
        broken["detail_with_bytes"] = b"\x00not-json"
        with pytest.raises(TypeError):
            sqlite_store.append("proj", broken)

    with _connect(store) as connection:
        durable = connection.execute(
            'SELECT "run_id" FROM "runs" WHERE "run_id" = ?', ("r-broken",)
        ).fetchone()
        detail = connection.execute(
            'SELECT "run_id" FROM "run_details" WHERE "run_id" = ?', ("r-broken",)
        ).fetchone()
        assert durable is None
        assert detail is None


# ── The store itself is not a reader; the declared durable split holds ─────


def test_the_wide_detail_row_carries_the_full_record(
    store: Path,
) -> None:
    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append("proj", _record("r-full", outcome="the whole story"))

    with _connect(store) as connection:
        row = connection.execute(
            'SELECT "detail" FROM "run_details" WHERE "run_id" = ?', ("r-full",)
        ).fetchone()
        detail = json.loads(row["detail"])
    assert detail["run_id"] == "r-full"
    assert detail["outcome"] == "the whole story"
    assert detail["project"] == "proj"
    # The durable half holds the narrow fields only.
    with _connect(store) as connection:
        durable = connection.execute(
            'SELECT * FROM "runs" WHERE "run_id" = ?', ("r-full",)
        ).fetchone()
    assert set(dict(durable)) == {name for name, _sql in run_store.DURABLE_FIELDS}
