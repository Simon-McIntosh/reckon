"""The embedded run store: detail classified, everything else durable.

The store is a shadow beside the committed ledger file: nothing reads it yet,
and nothing in this module reads it either — only the append a promotion
makes, the schema it creates on first use, the durable/detail readers the
rotation contract asserts against, and the path resolver the isolation
assertions use. These tests pin the contract that makes rotation delete only
detail: the single ``DETAIL_FIELDS`` declaration names the wide fields a
later rotation may wash out and every other field on a run record is durable
by default, the durable half round-trips every field with its type intact,
and washing every detail row away leaves the durable half answering every
field it carried. Every test's store resolves to a temporary directory
leaving the real crew config home untouched.
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


def _addressing_record(run_id: str, **overrides: object) -> dict:
    """A minimal record whose only durable fields are identity and one gate."""
    record = {
        "run_id": run_id,
        "member": "worker-a",
        "gate": "passed",
        "completed_at": "2026-09-09T00:00:00Z",
    }
    record.update(overrides)
    return record


def _real_record(run_id: str) -> dict:
    """A record shaped like the rows a promotion builds and writes.

    The field set mirrors the durable fields ``build_record`` assembles plus
    the thirteen wide fields that are classified as washable detail, carrying
    the same type mix a real row does: nested mappings, lists, bools, ints,
    floats and nulls. Deriving expectations from this same fixture in each
    test keeps the assertion field-by-field rather than a snapshot.
    """
    return {
        "run_id": run_id,
        "plan": "history-persists-as-detail-washes-out",
        "section": "s2",
        "node": "node-a",
        "role": "implement",
        "spec_level": "guided",
        "member": "worker-a",
        "backend": "clive",
        "local": False,
        "agent": {
            "harness": "native",
            "model": "sonnet",
            "nested": {"enabled": True, "depth": 2},
        },
        "dispatched_at": "2026-09-09T00:00:00Z",
        "completed_at": "2026-09-09T00:05:00Z",
        "completed_at_source": "promotion_time",
        "worker_seconds": 120,
        "worker_seconds_source": "stream_events",
        "wall_seconds": 300,
        "stalled": False,
        "time_budget": "50m",
        "base_sha": "aaaa1111",
        "commits": ["abc123", "def456"],
        "changed_lines": {"reckon/run_store.py": 42, "tests/test_run_store.py": 9},
        "tests_added": 7,
        "gate": "passed",
        "failure_classification": None,
        "outcome": "landed",
        "manifest_path": "run-dir/manifest.md",
        "scope_changed": False,
        "session_id": None,
        "lineage": {"kind": "dispatch", "predecessor": None},
        "shadow_controlled": None,
        "budget_fallback": {"tokens": {"reason": "not-a-restriction"}},
        "attempt": 1,
        "attempt_kind": "dispatch",
        "dispute_count": 0,
        "predecessor_run": None,
        # The thirteen wide fields measured at 78.3 percent of payload.
        "node_definition": {
            "id": "node-a",
            "plan": "history-persists-as-detail-washes-out",
            "spec_level": "guided",
            "goal": "classify the wide detail rather than the narrow record",
            "fence": ["reckon/run_store.py", "tests/test_run_store.py"],
        },
        "budget": {
            "tokens": {
                "input": 1000,
                "output": 500,
                "input_cumulative": 5000,
                "output_cumulative": 2500,
            },
            "cost_usd": 0.0042,
        },
        "unreconciled_override": {"gate": "refused", "reason": "seen on stream"},
        "gate_check": {
            "command": "uv run pytest tests/test_run_store.py",
            "exit_status": 0,
            "log_path": "run-dir/gate.log",
            "log_digest": "aabbcc",
        },
        "execution_fit": {"fits": True, "reason": "dedicated worktree"},
        "throughput": {"tokens_per_minute": 150.5, "window": "five_hour"},
        "worktree_retention": {"kept": True, "reason": "resume path"},
        "lane_receipt": {
            "lane": "clive",
            "window_utilization": 0.05,
            "resets_at": "2026-09-10T10:00:00Z",
        },
        "suite_delta": {"added": 0, "removed": 4, "base_revision": "5ce06a7"},
        "resume_remedy": {"session": "s-123", "remedy": "resume"},
        "failure_attribution": {
            "failure_id": "xx",
            "candidate_commit": "aaaa1111",
        },
        "shadow_patch": "--- a/reckon/flag.py\n+++ b/reckon/flag.py\n@@ -1 +1 @@\n",
        "follow_on_paths": ["docs/plans/other.html", "reckon/flag.py"],
    }


def _durable_expected(record: dict, *, project: str | None = None) -> dict:
    """The durable half a record resolves to: every field, minus declared detail.

    ``project`` is injected by the append when the record does not carry it,
    so a caller naming the project it appended under must include it in the
    expected durable set.
    """
    durable = {
        name: value
        for name, value in record.items()
        if name not in run_store.DETAIL_FIELDS
    }
    if project is not None:
        durable["project"] = project
    return durable


def _detail_expected(record: dict) -> dict:
    """The wide detail a record carries: the declared fields it has, nothing else."""
    return {
        name: value for name, value in record.items() if name in run_store.DETAIL_FIELDS
    }


def _assert_answers_every_field(durable: dict, expected: dict, *, where: str) -> None:
    """Assert the durable half answers the expected fields, field by field.

    Each field is checked for value and for Python type, so a nested mapping
    that collapsed to a string or a boolean that widened to an int fails
    rather than lucking through an equality that ignores shape.
    """
    assert set(durable) == set(expected), (
        f"{where}: durable keys {sorted(durable)} do not match "
        f"expected {sorted(expected)}"
    )
    for name, value in expected.items():
        assert durable[name] == value, f"{where}: field {name!r} value differs"
        assert type(durable[name]) is type(value), (
            f"{where}: field {name!r} changed type from "
            f"{type(value).__name__} to {type(durable[name]).__name__}"
        )


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def _table_names(connection: sqlite3.Connection, *, kind: str = "table") -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
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
    run_store.append("proj", _addressing_record("r-one"))

    store_location = run_store.store_path()
    assert store_location.is_file()
    with _connect(store_location) as connection:
        tables = _table_names(connection)
        assert {"runs", "run_details", "members", "holds"} <= tables
        # The run row is split into a durable half and a washable detail half.
        durable_columns = {
            str(row["name"]) for row in connection.execute('PRAGMA table_info("runs")')
        }
        detail_columns = {
            str(row["name"])
            for row in connection.execute('PRAGMA table_info("run_details")')
        }
        assert {"run_id", "payload"} <= durable_columns
        assert {"run_id", "detail"} <= detail_columns

    # The real crew config home was not written to.
    assert real_store.exists() == was_present
    # The write landed under the temporary config home the suite provides.
    configured_home = os.environ.get("RECKON_HOME", "")
    assert configured_home and str(store_location).startswith(configured_home)


# ── The detail set is exactly the measured wide fields ─────────────────────


def test_the_detail_set_is_exactly_the_measured_wide_fields() -> None:
    assert len(run_store.DETAIL_FIELDS) == 13
    assert len(set(run_store.DETAIL_FIELDS)) == 13
    assert set(run_store.DETAIL_FIELDS) == {
        "node_definition",
        "budget",
        "unreconciled_override",
        "gate_check",
        "execution_fit",
        "throughput",
        "worktree_retention",
        "lane_receipt",
        "suite_delta",
        "resume_remedy",
        "failure_attribution",
        "shadow_patch",
        "follow_on_paths",
    }


# ── The classification is inverted: everything not declared is durable ─────


def test_an_unclassified_field_is_durable_by_default(
    store: Path,
) -> None:
    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append(
            "proj",
            _addressing_record(
                "r-new",
                brand_new_field={"shape": "unknown", "ever": True},
                another_fresh_field="kept",
            ),
        )
        durable = sqlite_store.get_run("r-new")
        detail = sqlite_store.get_detail("r-new")

    # Neither new field is declared washable, so both land in the durable half
    # and neither is washed out with the detail.
    assert durable["brand_new_field"] == {"shape": "unknown", "ever": True}
    assert durable["another_fresh_field"] == "kept"
    assert "brand_new_field" not in detail
    assert "another_fresh_field" not in detail
    # The detail is exactly the declared wide fields the record carried.
    assert set(detail) == {
        name for name in run_store.DETAIL_FIELDS if name in _addressing_record("r-new")
    }


# ── The durable half round-trips a real record, field by field ─────────────


def test_the_durable_half_round_trips_a_real_record_field_by_field(
    store: Path,
) -> None:
    record = _real_record("r-real")
    expected = _durable_expected(record, project="proj")

    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append("proj", record)
        durable = sqlite_store.get_run("r-real")
        detail = sqlite_store.get_detail("r-real")

    # Every field outside the declared detail set is answered by the durable
    # half with its type intact, nested mappings, nulls, bools and all.
    _assert_answers_every_field(durable, expected, where="durable half")
    # project is injected by the append, so it is durable too.
    assert durable["project"] == "proj"
    # The detail half carries exactly the declared wide fields, and no durable
    # field leaks into it.
    _assert_answers_every_field(detail, _detail_expected(record), where="detail half")
    for name in ("run_id", "member", "gate", "outcome", "plan", "project"):
        assert name not in detail


def test_a_nested_mapping_survives_as_a_mapping_not_a_string(
    store: Path,
) -> None:
    record = _real_record("r-nested")
    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append("proj", record)
        durable = sqlite_store.get_run("r-nested")

    assert type(durable["agent"]["nested"]) is dict
    assert durable["agent"]["nested"]["enabled"] is True
    # A float survives as a float, an int as an int, a bool as a bool.
    assert type(durable["changed_lines"]["reckon/run_store.py"]) is int
    assert type(durable["worker_seconds"]) is int


# ── Rotation washes detail; the durable half answers everything ────────────


def test_washing_every_detail_row_leaves_the_durable_half_answering(
    store: Path,
) -> None:
    records = {f"r-{suffix}": _real_record(f"r-{suffix}") for suffix in ("one", "two")}
    expected = {
        run_id: _durable_expected(record, project="proj")
        for run_id, record in records.items()
    }

    with run_store.RunStore(store) as sqlite_store:
        for record in records.values():
            sqlite_store.append("proj", record)
        with _connect(store) as connection:
            connection.execute('DELETE FROM "run_details"')

        # The simulate-a-rotation half: after a rotation deletes every detail
        # row, the durable half still answers every durable field it carried.
        for run_id in records:
            assert sqlite_store.get_detail(run_id) is None
            _assert_answers_every_field(
                sqlite_store.get_run(run_id),
                expected[run_id],
                where=f"{run_id} after rotation",
            )


# ── One transaction, and no other row is read or rewritten ─────────────────


def test_append_writes_one_durable_and_one_detail_row_and_leaves_others_alone(
    store: Path, tmp_path: Path
) -> None:
    real_store = _real_store_path()
    was_present = real_store.exists()

    with run_store.RunStore(store) as sqlite_store:
        for suffix in ("one", "two", "three"):
            sqlite_store.append("proj", _addressing_record(f"r-{suffix}"))

    with _connect(store) as connection:
        before = {
            str(row["run_id"]): str(row["payload"])
            for row in connection.execute(
                'SELECT "run_id", "payload" FROM "runs" ORDER BY "run_id"'
            )
        }
        detail_before = {
            str(row["run_id"]): str(row["detail"])
            for row in connection.execute(
                'SELECT "run_id", "detail" FROM "run_details" ORDER BY "run_id"'
            )
        }

    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append("proj", _addressing_record("r-four"))

    with _connect(store) as connection:
        after = {
            str(row["run_id"]): str(row["payload"])
            for row in connection.execute(
                'SELECT "run_id", "payload" FROM "runs" ORDER BY "run_id"'
            )
        }
        detail_after = {
            str(row["run_id"]): str(row["detail"])
            for row in connection.execute(
                'SELECT "run_id", "detail" FROM "run_details" ORDER BY "run_id"'
            )
        }

    # Every pre-existing row is byte-identical after the append.
    assert {run_id: after[run_id] for run_id in before} == before
    assert {run_id: detail_after[run_id] for run_id in detail_before} == detail_before
    # The new row landed in both halves.
    assert "r-four" in after
    assert "r-four" in detail_after
    assert "r-four" not in before

    assert real_store.exists() == was_present


def test_a_failed_detail_write_rolls_back_the_whole_append(
    store: Path,
) -> None:
    with run_store.RunStore(store) as sqlite_store:
        # The durable half inserts fine; the detail half cannot serialise the
        # record, so the single transaction must roll both back.
        broken = _addressing_record("r-broken")
        broken["gate_check"] = {"raw": b"\x00not-json"}
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


def test_a_failed_durable_write_rolls_back_the_whole_append(
    store: Path,
) -> None:
    with run_store.RunStore(store) as sqlite_store:
        # The durable half cannot serialise the record; the detail insert must
        # not survive on its own.
        broken = _addressing_record("r-broken")
        broken["manifest_path"] = b"\x00not-json"
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


# ── The query keys are real indexed columns beside the payload ──────────────


def _query_plan(connection: sqlite3.Connection, key: str) -> str:
    """The planner's own words for a lookup through one query key."""
    row = connection.execute(
        f'EXPLAIN QUERY PLAN SELECT "run_id" FROM "runs" WHERE "{key}" = ?',  # noqa: S608
        ("worker-a",),
    ).fetchone()
    return str(row[3])


def test_the_five_query_keys_are_answered_from_indexed_columns(store: Path) -> None:
    with run_store.RunStore(store) as sqlite_store:
        for suffix in ("one", "two", "three"):
            sqlite_store.append("proj", _addressing_record(f"r-{suffix}"))

    with _connect(store) as connection:
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'runs'"
            )
        }
        # run_id is indexed by the primary key; the other four keys each get
        # their own index from the declaration.
        assert "idx_runs_member" in indexes
        assert "idx_runs_node" in indexes
        assert "idx_runs_project" in indexes
        assert "idx_runs_completed_at" in indexes

        # For each of the five keys the planner reports a search using an
        # index rather than a table scan, asserted on the plan text rather
        # than on elapsed time. A SEARCH row in SQLite's query plan always
        # reads through an index — the primary key or a query-key index —
        # while a SCAN row is the whole-table scan the flat file costs.
        for key in ("run_id", "member", "node", "project", "completed_at"):
            plan = _query_plan(connection, key)
            assert "SCAN" not in plan, f"{key}: plan scans: {plan}"
            assert plan.startswith("SEARCH"), (
                f"{key}: plan does not search an index: {plan}"
            )


def test_a_query_for_one_member_returns_the_same_rows_either_way(
    store: Path,
) -> None:
    for suffix, member in (
        ("one", "worker-a"),
        ("two", "worker-b"),
        ("three", "worker-a"),
    ):
        record = _addressing_record(f"r-{suffix}")
        record["member"] = member
        with run_store.RunStore(store) as sqlite_store:
            sqlite_store.append("proj", record)

    with _connect(store) as connection:
        via_column = {
            str(row["run_id"])
            for row in connection.execute(
                'SELECT "run_id" FROM "runs" WHERE "member" = ?', ("worker-a",)
            )
        }
        via_payload = {
            str(row["run_id"])
            for row in connection.execute(
                'SELECT "run_id" FROM "runs" '
                'WHERE json_extract("payload", "$.member") = ?',
                ("worker-a",),
            )
        }

    assert via_column == {"r-one", "r-three"}
    assert via_column == via_payload


# ── A store from an earlier declaration migrates in place on open ───────────


def _build_legacy_shape(connection: sqlite3.Connection) -> None:
    """Create the runs table under the previous narrow-column declaration.

    The earlier shape had ten durable columns and no payload blob; this is
    the shape the live store carries its 22 rows in, and it is the shape
    whose every write is failing today because ``CREATE TABLE IF NOT EXISTS``
    leaves the old table exactly as it was.
    """
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
        CREATE INDEX "idx_runs_member" ON "runs" ("member");
        CREATE TABLE "run_details" (
            "run_id" TEXT PRIMARY KEY,
            "detail" TEXT NOT NULL
        );
        CREATE TABLE "members" (
            "member_id" TEXT PRIMARY KEY,
            "payload" TEXT NOT NULL
        );
        CREATE TABLE "holds" (
            "hold_id" TEXT PRIMARY KEY,
            "payload" TEXT NOT NULL
        );
        """
    )
    connection.execute(
        'INSERT INTO "runs" '
        '("run_id", "project", "member", "node", "gate", "completed_at") '
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("r-old", "proj", "worker-a", "node-a", "passed", "2026-09-09T00:00:00Z"),
    )


def test_a_store_from_the_narrow_column_shape_migrates_in_place_on_open(
    store: Path,
) -> None:
    with _connect(store) as connection:
        _build_legacy_shape(connection)

    # Opening with the current code migrates the table before the next write:
    # the append succeeds and the earlier row stays readable in full.
    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append("proj", _addressing_record("r-new"))
        old_run = sqlite_store.get_run("r-old")
        new_run = sqlite_store.get_run("r-new")

    assert old_run == {
        "run_id": "r-old",
        "project": "proj",
        "member": "worker-a",
        "node": "node-a",
        "gate": "passed",
        "completed_at": "2026-09-09T00:00:00Z",
    }
    assert new_run["run_id"] == "r-new"

    # The migrated table carries the query keys as real columns plus the
    # payload — the legacy narrow columns stay as harmless extras — with the
    # query-key indexes, and leaves no scaffold table behind.
    with _connect(store) as connection:
        columns = {
            str(row["name"]) for row in connection.execute('PRAGMA table_info("runs")')
        }
        assert {
            "run_id",
            "member",
            "node",
            "project",
            "completed_at",
            "payload",
        } <= columns
        assert "idx_runs_member" in _table_names(connection, kind="index")
        migrated = connection.execute(
            'SELECT "member", "node", "project", "completed_at" '
            'FROM "runs" WHERE "run_id" = ?',
            ("r-old",),
        ).fetchone()
        assert tuple(migrated) == ("worker-a", "node-a", "proj", "2026-09-09T00:00:00Z")
        assert "runs_legacy" not in _table_names(connection)


def test_a_store_from_the_payload_blob_shape_migrates_in_place_on_open(
    store: Path,
) -> None:
    # The immediately-preceding shape kept the durable half as one payload
    # blob but dropped the query-key columns.
    with _connect(store) as connection:
        connection.executescript(
            """
            CREATE TABLE "runs" (
                "run_id" TEXT PRIMARY KEY,
                "payload" TEXT NOT NULL
            );
            CREATE TABLE "run_details" (
                "run_id" TEXT PRIMARY KEY,
                "detail" TEXT NOT NULL
            );
            CREATE TABLE "members" (
                "member_id" TEXT PRIMARY KEY,
                "payload" TEXT NOT NULL
            );
            CREATE TABLE "holds" (
                "hold_id" TEXT PRIMARY KEY,
                "payload" TEXT NOT NULL
            );
            """
        )
        connection.execute(
            'INSERT INTO "runs" ("run_id", "payload") VALUES (?, ?)',
            (
                "r-old",
                json.dumps(
                    {
                        "run_id": "r-old",
                        "project": "proj",
                        "member": "worker-a",
                        "node": "node-a",
                        "gate": "passed",
                        "completed_at": "2026-09-09T00:00:00Z",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )

    with run_store.RunStore(store) as sqlite_store:
        sqlite_store.append("proj", _addressing_record("r-new"))
        old_run = sqlite_store.get_run("r-old")

    assert old_run["member"] == "worker-a"
    assert old_run["node"] == "node-a"
    with _connect(store) as connection:
        row = connection.execute(
            'SELECT "member" FROM "runs" WHERE "run_id" = ?', ("r-old",)
        ).fetchone()
        assert str(row["member"]) == "worker-a"


# ── The corpus import and the standing equality check ──────────────────────
#
# The dual-write stage's second half: a re-runnable import brings the whole
# committed ledger, resolved by project name through the ordinary ledger
# loader, into the store beside the rows promotions already wrote, and the
# equality check compares the two stores project by project so dual-write can
# be believed rather than assumed. The store never carries the promotion
# record (``store_write`` is what happened at one promotion, not current
# state), and the import leaves already-identical rows untouched, which is what
# makes a second pass report zero of both numbers. Every test points the
# config home at a temp tree and seeds the committed ledger there, so the real
# crew config home is never touched.


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config home at a temp tree for import/equality tests."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _seed_ledger(project: str, runs: list[dict]) -> None:
    """Write a committed crew ledger for one project under the temp home."""
    from reckon import ledger

    ledger.write(project, {"members": [], "runs": runs, "holds": []}, 0)


def test_import_brings_every_ledger_run_into_the_store_and_reports_imported(
    config_home: Path,
) -> None:
    real_store = _real_store_path()
    was_present = real_store.exists()

    records = {
        f"r-{suffix}": _real_record(f"r-{suffix}") for suffix in ("one", "two", "three")
    }
    _seed_ledger("reckon", list(records.values()))

    report = run_store.import_ledger("reckon")

    assert report["project"] == "reckon"
    assert report["rows_imported"] == 3
    assert report["rows_already_present"] == 0
    assert report["rows_unchanged"] == 0
    # Every committed run now answers from the store, field for field, with
    # project injected the way a live promotion would inject it.
    with run_store.RunStore() as store:
        for run_id, record in records.items():
            _assert_answers_every_field(
                store.get_run(run_id),
                _durable_expected(record, project="reckon"),
                where=f"{run_id} after import",
            )
    # The real crew config home was not written to.
    assert real_store.exists() == was_present


def test_a_second_import_pass_reports_zero_of_both(config_home: Path) -> None:
    _seed_ledger("reckon", [_real_record("r-one"), _real_record("r-two")])

    first = run_store.import_ledger("reckon")
    assert first["rows_imported"] == 2

    second = run_store.import_ledger("reckon")
    assert second["rows_imported"] == 0
    assert second["rows_already_present"] == 0
    assert second["rows_unchanged"] == 2


def test_import_corrects_a_row_already_present_with_a_stale_durable_record(
    config_home: Path,
) -> None:
    # The store already holds this run under the old narrow shape, as a subset
    # of the record the ledger now carries — exactly the rows rebuilt from the
    # ten original columns before the payload existed.
    with run_store.RunStore() as store:
        store.append("reckon", _addressing_record("r-legacy"))
    record = _real_record("r-legacy")
    _seed_ledger("reckon", [record])

    report = run_store.import_ledger("reckon")

    assert report["rows_imported"] == 0
    assert report["rows_already_present"] == 1
    assert report["rows_unchanged"] == 0
    # The stale subset was corrected to the full durable record the ledger
    # carries.
    with run_store.RunStore() as store:
        _assert_answers_every_field(
            store.get_run("r-legacy"),
            _durable_expected(record, project="reckon"),
            where="r-legacy after correction",
        )


def test_import_never_rewrites_the_historical_store_write(config_home: Path) -> None:
    from reckon import ledger

    failed_a = {
        **_real_record("r-failed-a"),
        "store_write": {"status": "failed", "error": "deliberate"},
    }
    failed_b = {
        **_real_record("r-failed-b"),
        "store_write": {"status": "failed", "error": "deliberate"},
    }
    written = {**_real_record("r-written"), "store_write": {"status": "written"}}
    _seed_ledger("reckon", [failed_a, failed_b, written])

    run_store.import_ledger("reckon")

    # The committed rows still carry the historical failure record: the import
    # only reads the file, and it never writes store_write into the store.
    data, _version = ledger.load("reckon")
    statuses = {
        entry["run_id"]: entry["store_write"]["status"] for entry in data["runs"]
    }
    assert statuses == {
        "r-failed-a": "failed",
        "r-failed-b": "failed",
        "r-written": "written",
    }
    with run_store.RunStore() as store:
        for run_id in ("r-failed-a", "r-failed-b", "r-written"):
            assert "store_write" not in store.get_run(run_id)
    # The equality check is undisturbed by the promotion record, so the two
    # failed rows do not masquerade as divergence.
    assert run_store.compare("reckon")["rows_present_in_both_disagreeing"] == 0


def test_equality_check_reports_zero_on_all_three_after_import(
    config_home: Path,
) -> None:
    records = {
        f"r-{suffix}": _real_record(f"r-{suffix}") for suffix in ("one", "two", "three")
    }
    _seed_ledger("reckon", list(records.values()))
    run_store.import_ledger("reckon")

    verdict = run_store.compare("reckon")

    assert verdict["rows_in_file_absent_from_store"] == 0
    assert verdict["rows_in_store_absent_from_file"] == 0
    assert verdict["rows_present_in_both_disagreeing"] == 0


def test_equality_check_reports_a_row_removed_from_the_store(
    config_home: Path,
) -> None:
    records = {
        f"r-{suffix}": _real_record(f"r-{suffix}") for suffix in ("one", "two", "three")
    }
    _seed_ledger("reckon", list(records.values()))
    run_store.import_ledger("reckon")
    with _connect(run_store.store_path()) as connection:
        connection.execute('DELETE FROM "runs" WHERE "run_id" = ?', ("r-one",))
        connection.execute('DELETE FROM "run_details" WHERE "run_id" = ?', ("r-one",))

    verdict = run_store.compare("reckon")

    assert verdict["rows_in_file_absent_from_store"] == 1
    assert verdict["file_only_run_ids"] == ["r-one"]
    assert verdict["rows_in_store_absent_from_file"] == 0
    assert verdict["rows_present_in_both_disagreeing"] == 0


def test_equality_check_reports_a_store_row_absent_from_the_file(
    config_home: Path,
) -> None:
    _seed_ledger("reckon", [_real_record("r-one")])
    run_store.import_ledger("reckon")
    # A store-only row the file never committed — the orphan direction the
    # check must detect, not only the missing-row direction.
    with run_store.RunStore() as store:
        store.append("reckon", _addressing_record("r-orphan"))

    verdict = run_store.compare("reckon")

    assert verdict["rows_in_store_absent_from_file"] == 1
    assert verdict["store_only_run_ids"] == ["r-orphan"]
    assert verdict["rows_in_file_absent_from_store"] == 0
    assert verdict["rows_present_in_both_disagreeing"] == 0


def test_equality_check_catches_a_field_the_store_silently_dropped(
    config_home: Path,
) -> None:
    records = {f"r-{suffix}": _real_record(f"r-{suffix}") for suffix in ("one", "two")}
    _seed_ledger("reckon", list(records.values()))
    run_store.import_ledger("reckon")
    # A writer that drops one durable field entirely, and one that lets a value
    # drift: both must count as disagreement, because the comparison covers
    # every field the store calls durable rather than a chosen subset.
    with _connect(run_store.store_path()) as connection:
        row = connection.execute(
            'SELECT "payload" FROM "runs" WHERE "run_id" = ?', ("r-one",)
        ).fetchone()
        payload = json.loads(str(row["payload"]))
        payload.pop("gate")
        connection.execute(
            'UPDATE "runs" SET "payload" = ? WHERE "run_id" = ?',
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), "r-one"),
        )
        row = connection.execute(
            'SELECT "payload" FROM "runs" WHERE "run_id" = ?', ("r-two",)
        ).fetchone()
        payload = json.loads(str(row["payload"]))
        payload["gate"] = "refused"
        connection.execute(
            'UPDATE "runs" SET "payload" = ? WHERE "run_id" = ?',
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), "r-two"),
        )

    verdict = run_store.compare("reckon")

    assert verdict["rows_present_in_both_disagreeing"] == 2
    assert verdict["disagreeing_run_ids"] == sorted(["r-one", "r-two"])
    assert verdict["rows_in_file_absent_from_store"] == 0
    assert verdict["rows_in_store_absent_from_file"] == 0


def test_equality_check_scopes_the_store_by_project(config_home: Path) -> None:
    # The database is shared across every project on a config home; a row a
    # different project promoted must not read as a divergence for this one.
    with run_store.RunStore() as store:
        store.append("otherproj", _addressing_record("r-other"))
    _seed_ledger("reckon", [_real_record("r-one")])
    run_store.import_ledger("reckon")

    verdict = run_store.compare("reckon")

    assert verdict["rows_in_store_absent_from_file"] == 0
    assert verdict["rows_in_file_absent_from_store"] == 0
    assert verdict["rows_present_in_both_disagreeing"] == 0
