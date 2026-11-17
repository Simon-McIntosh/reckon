"""Plan selection searches both stores without decoding unrelated history."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import ledger, run_store


def _reference(path: Path, plan: str = "selected") -> list[dict]:
    return [
        row
        for row in run_store.stored_run_rows("project", path=path)
        if ledger.row_plan(row) == plan
    ]


def _assert_reference(actual: list[dict], expected: list[dict]) -> None:
    assert actual == expected


def _retire(path: Path) -> list[str]:
    with run_store.RunStore(path) as store:
        return store.retire(
            before=datetime(2001, 1, 1, tzinfo=UTC),
            protected=set(),
            projects={"project"},
            apply=True,
        )["retired"]


@pytest.fixture(scope="module")
def population(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("population") / "runs.db"
    with run_store.RunStore(path) as store:
        # Distractors use the declared insert; all selected identities exercise
        # the public writer so fixture construction cannot supply their index.
        records = (
            {
                "run_id": f"other-{number:05d}",
                "project": "project",
                "plan": "unrelated",
                "completed_at": "2026-01-01T00:00:00Z",
                "outcome": "unrelated history " * 64,
            }
            for number in range(19950)
        )
        with store._conn:
            store._conn.executemany(
                run_store._RUNS_INSERT,
                (
                    (
                        row["run_id"],
                        *(row.get(key) for key in run_store.QUERY_KEYS),
                        None,
                        json.dumps(row),
                    )
                    for row in records
                ),
            )
        for number in range(50):
            row = {
                "run_id": f"match-{number:02d}",
                "node": "candidate",
                "completed_at": (
                    "2000-01-01T00:00:00Z" if number % 2 else "2026-01-01T00:00:00Z"
                ),
                "outcome": "selected history " * 64,
            }
            if number % 3 == 0:
                row["plan"] = " selected "
            elif number % 3 == 1:
                row["impl_move"] = {"plan": " selected "}
            else:
                row["node_definition"] = {"plan": " selected ", "goal": "read"}
            store.append("project", row)
    assert len(_retire(path)) == 25
    assert len(run_store.stored_run_rows("project", path=path)) == 20000
    return path


@pytest.fixture()
def history(population: Path, tmp_path: Path) -> Path:
    path = tmp_path / "runs.db"
    shutil.copyfile(population, path)
    shutil.copyfile(run_store._archive_path(population), run_store._archive_path(path))
    return path


@pytest.mark.parametrize("fields", [None, ("run_id",), ("run_id", "node_definition")])
def test_selected_read_visits_only_matching_payloads(history, monkeypatch, fields):
    started = time.perf_counter()
    expected = _reference(history)
    exhaustive_seconds = time.perf_counter() - started
    assert len(expected) == 50
    decoded = 0

    def loads(value, *args, **kwargs):
        nonlocal decoded
        decoded += 1
        return json.loads(value, *args, **kwargs)

    monkeypatch.setattr(run_store, "json", SimpleNamespace(loads=loads))
    started = time.perf_counter()
    actual = run_store.stored_run_rows(
        "project", path=history, plan="selected", fields=fields
    )
    selected_seconds = time.perf_counter() - started
    if fields is not None:
        expected = [{key: row[key] for key in fields if key in row} for row in expected]
    _assert_reference(actual, expected)
    print(
        f"population=20000 matches=50 decoded={decoded} "
        f"selected_seconds={selected_seconds:.6f} exhaustive_seconds={exhaustive_seconds:.6f}"
    )
    assert decoded <= 4 * len(expected), f"decoded {decoded} payloads for 50 matches"
    assert selected_seconds < exhaustive_seconds / 2


def test_selection_composes_with_identity_node_and_activity(history):
    expected = _reference(history)
    actual = run_store.stored_run_rows(
        "project",
        path=history,
        plan="selected",
        node="candidate",
        run_ids=[row["run_id"] for row in expected[:12]] + ["other-00000"],
        activity_since=datetime(2025, 1, 1, tzinfo=UTC).timestamp(),
    )
    _assert_reference(
        actual,
        [row for row in expected[:12] if row["completed_at"].startswith("2026")],
    )
    assert run_store.stored_run_rows("foreign", path=history, plan="selected") == []
    assert run_store.stored_run_rows("project", path=history, plan="absent") == []


@pytest.mark.parametrize(
    "identity",
    [
        {"plan": " selected ", "impl_move": {"plan": "ignored"}},
        {"plan": "  ", "impl_move": {"plan": "selected"}},
        {"impl_move": {"plan": " selected "}, "node_definition": {"plan": "ignored"}},
        {"impl_move": None, "node_definition": {"plan": "selected"}},
        {"plan": 42},
        {},
    ],
)
def test_writes_keep_the_readers_plan_identity(tmp_path, identity):
    path = tmp_path / "runs.db"
    row = {"run_id": "run", **identity}
    with run_store.RunStore(path) as store:
        store.append("project", row)
    selected = ledger.row_plan(row)
    if selected:
        _assert_reference(
            run_store.stored_run_rows("project", path=path, plan=selected),
            [{"project": "project", **row}],
        )
    assert run_store.stored_run_rows("project", path=path, plan="ignored") == []


def test_identity_amendment_replacement_and_retirement_are_visible(history, tmp_path):
    for run_id in ("match-00", "match-01"):
        row = {"run_id": run_id, "node_definition": {"plan": "changed"}}
        with run_store.RunStore(history) as store:
            assert store.sync_run("project", row) == "updated"
        _assert_reference(
            run_store.stored_run_rows("project", path=history, plan="changed"),
            _reference(history, "changed"),
        )
    fresh = {"run_id": "fresh", "impl_move": {"plan": "selected"}}
    with run_store.RunStore(history) as store:
        store.append("project", fresh)
    before = _reference(history)
    fresh["completed_at"] = "2000-01-01T00:00:00Z"
    with run_store.RunStore(history) as store:
        store.sync_run("project", fresh)
    assert _retire(history) == ["fresh"]
    actual = run_store.stored_run_rows("project", path=history, plan="selected")
    _assert_reference(actual, _reference(history))
    assert {row["run_id"] for row in actual} == {row["run_id"] for row in before}

    replacement = tmp_path / "replacement.db"
    with run_store.RunStore(replacement) as store:
        store.append("project", {"run_id": "replacement", "plan": "selected"})
    replacement.replace(history)
    # The archive belongs to the replaced path and remains part of its history.
    _assert_reference(
        run_store.stored_run_rows("project", path=history, plan="selected"),
        _reference(history),
    )
    assert any(
        row["run_id"] == "replacement"
        for row in run_store.stored_run_rows("project", path=history, plan="selected")
    )


def test_migration_indexes_derived_identity_in_hot_and_archive(tmp_path, monkeypatch):
    path = tmp_path / "runs.db"
    for target in (path, run_store._archive_path(path)):
        with sqlite3.connect(target) as connection:
            connection.executescript(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, payload TEXT);"
                "CREATE TABLE run_details (run_id TEXT PRIMARY KEY, detail TEXT);"
            )
            for number, identity in enumerate(
                (
                    {"plan": "selected"},
                    {"impl_move": {"plan": "selected"}},
                    {"node_definition": {"plan": "selected"}},
                )
            ):
                row = {
                    "run_id": f"{target.stem}-{number}",
                    "project": "project",
                    **identity,
                }
                detail = (
                    {"node_definition": row.pop("node_definition")}
                    if "node_definition" in row
                    else {}
                )
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?)",
                    (row["run_id"], "project", json.dumps(row)),
                )
                connection.execute(
                    "INSERT INTO run_details VALUES (?, ?)",
                    (row["run_id"], json.dumps(detail)),
                )
    expected = _reference(path)
    with run_store.RunStore(path):
        pass
    _assert_reference(
        run_store.stored_run_rows("project", path=path, plan="selected"), expected
    )
    assert len(expected) == 6
    for target in (path, run_store._archive_path(path)):
        with sqlite3.connect(target) as connection:
            explanation = connection.execute(
                "EXPLAIN QUERY PLAN SELECT run_id FROM runs WHERE plan = ?",
                ("selected",),
            ).fetchall()
        assert any(
            "SEARCH" in row[3] and "idx_runs_plan" in row[3] for row in explanation
        )

    def unexpected_migration(*args, **kwargs):
        pytest.fail("an unchanged store must not repeat either migration")

    monkeypatch.setattr(run_store.RunStore, "_migrate_runs", unexpected_migration)
    with run_store.RunStore(path):
        pass


@pytest.mark.parametrize("broken", ["hot", "archive"])
def test_unreadable_history_is_refused(history, broken):
    target = history if broken == "hot" else run_store._archive_path(history)
    target.write_bytes(b"not a sqlite database")
    with pytest.raises(RuntimeError, match="cannot read the run store"):
        run_store.stored_run_rows("project", path=history, plan="selected")


def test_missing_archived_match_is_rejected_by_reference(history):
    expected = _reference(history)
    incomplete = [row for row in expected if row["run_id"] != "match-01"]
    assert len(incomplete) == len(expected) - 1
    actual = run_store.stored_run_rows("project", path=history, plan="selected")
    with pytest.raises(AssertionError):
        _assert_reference(actual, incomplete)
    print("reference refused: missing archived match-01")
