"""Consuming selected records must not extend SQLite's read lock."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime

import pytest

from reckon import run_store


@pytest.mark.parametrize("reader", ["stored", "projected", "indexed"])
def test_a_store_write_completes_during_a_long_read(tmp_path, monkeypatch, reader):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RECKON_RUN_STORE", raising=False)
    path = tmp_path / "runs.db"
    rows = [
        {
            "run_id": f"run-{index}",
            "project": "example",
            "plan": "sample",
            "completed_at": "2020-01-01T00:00:00Z",
            "node_definition": {"goal": f"goal-{index}"},
        }
        for index in range(10)
    ]
    with run_store.RunStore(path) as store:
        for row in rows:
            store.append("example", row)
        store.retire(
            before=datetime(2021, 1, 1, tzinfo=UTC),
            protected={row["run_id"] for row in rows[2:]},
            projects={"example"},
            apply=True,
        )
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(
            run_store.RECORD_BUSY_TIMEOUT_SECONDS * 1000
        )
        assert run_store.RECORD_BUSY_TIMEOUT_SECONDS == 120.0

    def read():
        if reader == "indexed":
            with run_store.RunStore(path) as store:
                return list(store._indexed_records(include_archive=True).values())
        kwargs = (
            {"plan": "sample", "fields": ("run_id", "project", "plan")}
            if reader == "projected"
            else {}
        )
        return run_store.stored_run_rows("example", path=path, **kwargs)

    expected = read()
    assert len(expected) == len(rows)
    assert {row["run_id"] for row in expected} == {row["run_id"] for row in rows}
    consuming = threading.Event()
    release = threading.Event()
    normalize = run_store.run_record.normalize_outside_scope_paths

    def pause_consumer(row):
        if not consuming.is_set():
            consuming.set()
            assert release.wait(15), "consumer was not released"
        return normalize(row)

    monkeypatch.setattr(
        run_store.run_record, "normalize_outside_scope_paths", pause_consumer
    )
    answer = []
    errors = []

    def consume():
        try:
            answer.extend(read())
        except Exception as error:  # noqa: BLE001 - assert thread errors in the caller
            errors.append(error)

    thread = threading.Thread(target=consume)
    thread.start()
    try:
        assert consuming.wait(10), "reader did not reach record consumption"
        started = time.monotonic()
        write = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, sys; from reckon import run_store; "
                    "store = run_store.RunStore(sys.argv[1], busy_timeout=1.0); "
                    "store.append('example', {'run_id': 'concurrent-write'}); "
                    "print(json.dumps(store.get_run('concurrent-write'))); store.close()"
                ),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        elapsed = time.monotonic() - started
        # Keep the consumer paused beyond the writer's contention bound.
        release.wait(max(0, 3.0 - elapsed))
        assert thread.is_alive(), "read consumption ended before the write receipt"
        assert write.returncode == 0, write.stderr
        assert json.loads(write.stdout)["run_id"] == "concurrent-write"
        print(f"{reader}: committed in {elapsed:.3f}s while consumption held >=3s")
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    assert sorted(answer, key=lambda row: row["run_id"]) == sorted(
        expected, key=lambda row: row["run_id"]
    )
    with run_store.RunStore(path) as store:
        assert store.get_run("concurrent-write")["run_id"] == "concurrent-write"
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
