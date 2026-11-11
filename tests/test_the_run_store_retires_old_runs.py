"""Finished history leaves current-state tables without losing its measurements."""

from __future__ import annotations

import json
import sqlite3
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
