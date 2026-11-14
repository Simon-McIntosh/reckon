"""Finished history leaves current-state tables without losing its measurements."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from reckon import flight, ledger, run_store
from reckon.crew import gc, summary


@pytest.fixture
def history(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "repo"
    state = root / "docs" / "state" / "sample"
    state.mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(home / "crew" / "run_store.db"))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(root / "docs" / "state"))
    monkeypatch.setattr(
        flight, "mounted_project_docs", lambda: {"sample": root / "docs"}
    )
    monkeypatch.setattr(gc, "settle_run_scratch", lambda **kw: {"removed": []})
    monkeypatch.setattr(gc, "_orphan_ledger_indexes", lambda **kw: [])
    monkeypatch.setattr(gc, "_expired_picker_views", lambda **kw: [])
    pointers = []
    monkeypatch.setattr(gc, "list_live", lambda: list(pointers))
    now = datetime(2026, 11, 1, tzinfo=UTC)
    ended = now - timedelta(days=31)
    records = [
        {
            "run_id": f"r-finished-{index}",
            "project": "sample",
            "node": f"task-{index}",
            "dispatched_at": (ended - timedelta(seconds=60)).isoformat(),
            "completed_at": ended.isoformat(),
            "wall_seconds": 60,
            "tool_steps": 7,
            "orientation_input_tokens": 100,
            "gate": "passed",
            "node_definition": {"brief": "detail" * 4000},
        }
        for index in range(24)
    ]
    (state / "crew.json").write_text(
        json.dumps({"data": {"members": [], "runs": records, "holds": []}})
    )
    with run_store.RunStore(root=root) as store:
        for record in records:
            store.append("sample", record)
    return root, now, records, pointers


def hot_ids():
    with sqlite3.connect(run_store.store_path()) as connection:
        return {row[0] for row in connection.execute("SELECT run_id FROM runs")}


def test_an_unneeded_run_leaves_hot_tables_and_history_stays_readable(history):
    root, now, records, _ = history
    with sqlite3.connect(run_store.store_path()) as connection:
        before = connection.execute("PRAGMA page_count").fetchone()[0]
    gc.retention_sweep(apply=True, now=now)
    assert not hot_ids(), "run ended 31 days ago remains in the hot tables"
    with sqlite3.connect(run_store.store_path()) as connection:
        assert connection.execute("SELECT count(*) FROM run_details").fetchone()[0] == 0
        after = connection.execute("PRAGMA page_count").fetchone()[0]
    assert after < before
    print(
        f"hot_pages_before={before} hot_pages_after={after} retired_runs={len(records)}"
    )
    with run_store.RunStore(root=root) as store:
        assert store.get_run(records[0]["run_id"])["tool_steps"] == 7
        assert store.get_detail(records[0]["run_id"]) == {
            "node_definition": records[0]["node_definition"]
        }
        assert store.records() == {}
        assert len(store.durable_rows("sample")) == len(records)
    assert len(ledger.runs("sample", root=root)) == len(records)
    assert summary.run_rows("sample", root=root)[0]["wall_seconds"] == 60
    assert not hot_ids(), "a historical read rehydrated retired rows"


@pytest.mark.parametrize("guard", ["live", "review", "queue", "review-owed"])
def test_a_run_something_can_still_need_stays_hot(history, monkeypatch, guard):
    _, now, records, pointers = history
    run_id = records[0]["run_id"]
    if guard == "live":
        pointers.append({"run_id": run_id})
    elif guard == "review":
        pointers.append({"run_id": "r-reviewer", "node": {"id": f"review-of-{run_id}"}})
    elif guard == "queue":
        pointers.append(
            {"run_id": "r-waiter", "phase": "queued", "waits": [f"run:{run_id}"]}
        )
    else:
        records[0]["review_tier"] = "paired"
        with run_store.RunStore() as store:
            store.sync_run("sample", records[0])
        monkeypatch.setattr(gc, "_committed_review_exists", lambda *args: False)
    gc.retention_sweep(apply=True, now=now)
    assert run_id in hot_ids()
    assert len(hot_ids()) == 1
    if guard == "review-owed":
        monkeypatch.setattr(gc, "_committed_review_exists", lambda *args: True)
        gc.retention_sweep(apply=True, now=now)
        assert not hot_ids()


def test_a_dry_sweep_and_a_recent_or_undated_run_stay_hot(history):
    _, now, records, _ = history
    with run_store.RunStore() as store:
        for key, stamp in (("r-recent", now.isoformat()), ("r-undated", None)):
            store.append("sample", {"run_id": key, "completed_at": stamp})
    gc.retention_sweep(apply=False, now=now)
    assert len(hot_ids()) == len(records) + 2
    gc.retention_sweep(apply=True, now=now)
    assert hot_ids() == {"r-recent", "r-undated"}


def test_import_and_membership_checks_do_not_repopulate_retired_history(history):
    root, now, records, _ = history
    gc.retention_sweep(apply=True, now=now)
    assert run_store.import_ledger("sample", root=root)["rows_unchanged"] == len(
        records
    )
    assert not hot_ids(), "import_ledger brought archived runs back into the hot tables"
    with sqlite3.connect(run_store.store_path()) as connection:
        assert connection.execute("SELECT count(*) FROM run_details").fetchone()[0] == 0
    assert ledger.index_lag("sample", root=root) == 0
    assert run_store.runs_for_node("task-0", project="sample")[0]["tool_steps"] == 7
    assert run_store.compare("sample", root=root)["rows_in_file_absent_from_store"] == 0
    assert not hot_ids()
    with run_store.RunStore(root=root) as store:
        assert not store._archive_attached
        assert store.records() == {}
        assert not store._archive_attached
        with pytest.raises(sqlite3.IntegrityError, match="already archived"):
            store.append("sample", records[0])


def test_a_failed_archive_copy_leaves_both_hot_tables_intact(history):
    root, now, records, _ = history
    with run_store.RunStore(root=root) as store:
        store._attach_archive(create=True)
        store._conn.execute(
            "CREATE TRIGGER archive.refuse_detail BEFORE INSERT ON run_details "
            "BEGIN SELECT RAISE(ABORT, 'archive unavailable'); END"
        )
        store._conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="archive unavailable"):
            store.retire(before=now, protected=set(), projects={"sample"}, apply=True)
        assert (
            store._conn.execute("SELECT count(*) FROM archive.runs").fetchone()[0] == 0
        )
        assert store._conn.execute("SELECT count(*) FROM run_details").fetchone()[
            0
        ] == len(records)
        assert not store._archived_ids()
    assert len(hot_ids()) == len(records)


def test_a_changed_ledger_keeps_archive_membership_and_updates_its_durable_fields(
    history,
):
    root, now, records, _ = history
    gc.retention_sweep(apply=True, now=now)
    state = root / "docs" / "state" / "sample" / "crew.json"
    records[0]["tool_steps"] = 19
    records[0]["node_definition"] = {"brief": "updated instructions"}
    records.pop()
    state.write_text(
        json.dumps({"data": {"members": [], "runs": records, "holds": []}})
    )
    with run_store.RunStore(root=root) as store:
        assert store.get_run(records[0]["run_id"])["tool_steps"] == 19
        assert store.get_detail(records[0]["run_id"]) == {
            "node_definition": records[0]["node_definition"]
        }
        assert len(store.durable_rows("sample")) == len(records)
        assert store.get_run("r-finished-23") is None
    assert not hot_ids()


def test_an_import_racing_retirement_does_not_recreate_a_hot_run(history, monkeypatch):
    root, now, records, _ = history
    inspected = threading.Event()
    proceed = threading.Event()

    def import_run():
        with run_store.RunStore(root=root) as importer:
            original = importer._is_archived

            def paused_membership(run_id):
                archived = original(run_id)
                inspected.set()
                assert proceed.wait(5), "retirement never released the importer"
                return archived

            monkeypatch.setattr(importer, "_is_archived", paused_membership)
            return importer.sync_run("sample", records[0])

    with run_store.RunStore(root=root, busy_timeout=0.05) as retiring:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(import_run)
            try:
                assert inspected.wait(5), "importer never inspected archive membership"
                try:
                    retiring.retire(
                        before=now, protected=set(), projects={"sample"}, apply=True
                    )
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc):
                        raise
            finally:
                proceed.set()
            future.result(timeout=10)
        retiring.retire(before=now, protected=set(), projects={"sample"}, apply=True)
        assert retiring.get_run(records[0]["run_id"])["tool_steps"] == 7
    assert not hot_ids(), "a racing importer recreated a retired run in the hot tables"


def test_a_default_sweep_moves_exactly_its_batch_and_leaves_the_rest(history):
    _, now, records, _ = history
    with run_store.RunStore() as store:
        backlog = [
            {
                "run_id": f"r-backlog-{index}",
                "project": "sample",
                "completed_at": records[0]["completed_at"],
            }
            for index in range(2003 - len(records))
        ]
        with store._conn:
            store._conn.executemany(
                "INSERT INTO runs (run_id, project, completed_at, payload) VALUES (?, ?, ?, ?)",
                [
                    (row["run_id"], "sample", row["completed_at"], json.dumps(row))
                    for row in backlog
                ],
            )
            store._conn.executemany(
                "INSERT INTO run_details (run_id, detail) VALUES (?, ?)",
                [(row["run_id"], "{}") for row in backlog],
            )
    report = gc.retention_sweep(apply=True, now=now)
    assert len(report["run_store"]["retired"]) == 2000
    assert len(hot_ids()) == 3
    next_report = gc.retention_sweep(apply=True, now=now)
    assert len(next_report["run_store"]["retired"]) == 3
    assert not hot_ids()


def test_the_hourly_sweep_uses_incremental_compaction_without_a_full_rewrite(
    history, monkeypatch
):
    _, now, _, _ = history
    statements = []
    original = run_store.RunStore

    def traced_store(*args, **kwargs):
        store = original(*args, **kwargs)
        store._conn.set_trace_callback(statements.append)
        return store

    monkeypatch.setattr(run_store, "RunStore", traced_store)
    gc.retention_sweep(apply=True, now=now)
    assert not [
        sql for sql in statements if sql.strip().upper().startswith("VACUUM")
    ], statements
    assert any("incremental_vacuum" in sql.lower() for sql in statements), statements


def test_the_command_configures_both_row_and_page_budgets(history, capsys):
    _, _, _, _ = history
    assert gc.main(["--apply", "--store-limit", "7", "--store-vacuum-pages", "3"]) == 0
    report = json.loads(capsys.readouterr().out)["run_store"]
    assert report["batch_limit"] == 7
    assert report["vacuum_page_limit"] == 3
    assert len(report["retired"]) == 7
    assert len(hot_ids()) == 17
    assert 0 < report["pages_before"] - report["pages_after"] <= 3


def test_incremental_compaction_continues_after_the_retirement_backlog_is_empty(
    history,
):
    _, now, _, _ = history
    first = gc.retention_sweep(apply=True, now=now, store_vacuum_pages=3)["run_store"]
    second = gc.retention_sweep(apply=True, now=now, store_vacuum_pages=3)["run_store"]
    assert len(first["retired"]) == 24
    assert not second["retired"]
    for report in (first, second):
        assert 0 < report["pages_before"] - report["pages_after"] <= 3


@pytest.mark.parametrize(("limit", "pages"), [(0, 3), (7, 0)])
def test_unbounded_store_budgets_are_refused(history, limit, pages):
    _, now, records, _ = history
    with (
        run_store.RunStore() as store,
        pytest.raises(ValueError, match="must be positive"),
    ):
        store.retire(
            before=now,
            protected=set(),
            projects={"sample"},
            apply=True,
            limit=limit,
            vacuum_pages=pages,
        )
    assert len(hot_ids()) == len(records)


def test_a_store_needing_conversion_is_not_rewritten_by_the_sweep(history, monkeypatch):
    _, now, records, _ = history
    legacy = run_store.store_path().with_name("unprepared.db")
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE resident (value TEXT)")
    monkeypatch.setenv("RECKON_RUN_STORE", str(legacy))
    with run_store.RunStore() as store:
        store.append("sample", records[0])
    report = gc.retention_sweep(apply=True, now=now)["run_store"]
    assert report["maintenance_required"] is True
    assert len(report["retired"]) == 1
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] > 0
    command = [
        sys.executable,
        "-m",
        "reckon.run_store",
        "--enable-incremental-vacuum",
        "--store",
        str(legacy),
    ]
    prepared = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=30
    )
    receipt = json.loads(prepared.stdout)
    assert receipt["auto_vacuum_before"] == 0
    assert receipt["auto_vacuum_after"] == 2
    assert receipt["rewritten"] is True
    repeated = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=30
    )
    assert json.loads(repeated.stdout)["rewritten"] is False
    with run_store.RunStore() as store:
        assert store.get_run(records[0]["run_id"])["tool_steps"] == 7
    assert (
        gc.retention_sweep(apply=True, now=now)["run_store"]["maintenance_required"]
        is False
    )
