"""A run-store connection waits for a held lock rather than failing fast."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from reckon import run_store


def _busy_timeout_ms(store: run_store.RunStore) -> int:
    return int(store._conn.execute("PRAGMA busy_timeout").fetchone()[0])


def test_a_store_opened_with_no_bound_waits_the_record_timeout(tmp_path: Path):
    with run_store.RunStore(tmp_path / "store.db") as store:
        assert _busy_timeout_ms(store) == int(
            run_store.RECORD_BUSY_TIMEOUT_SECONDS * 1000
        )


def test_a_stated_bound_is_kept(tmp_path: Path):
    with run_store.RunStore(tmp_path / "store.db", busy_timeout=0.2) as store:
        assert _busy_timeout_ms(store) == 200


def test_the_record_timeout_outlasts_the_default_wait():
    # sqlite's own default is five seconds, which a scan of the store exceeds.
    assert run_store.RECORD_BUSY_TIMEOUT_SECONDS >= 60


def test_a_write_lands_once_a_lock_held_past_a_short_wait_is_released(
    tmp_path: Path,
):
    path = tmp_path / "store.db"
    with run_store.RunStore(path):
        pass
    holder = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")

    def release() -> None:
        time.sleep(1.0)
        holder.execute("COMMIT")

    impatient = sqlite3.connect(str(path), timeout=0.05)
    refused = False
    try:
        impatient.execute("CREATE TABLE never_written (id INTEGER)")
    except sqlite3.OperationalError:
        refused = True
    finally:
        impatient.close()

    thread = threading.Thread(target=release)
    thread.start()
    try:
        with run_store.RunStore(path) as store:
            store._conn.execute("CREATE TABLE IF NOT EXISTS waited (id INTEGER)")
            store._conn.commit()
            tables = {
                row[0]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
    finally:
        thread.join()
        holder.close()

    assert refused
    assert "waited" in tables
    assert "never_written" not in tables
