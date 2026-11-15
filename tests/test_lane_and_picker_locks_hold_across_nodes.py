"""The picker's hold log and the catalogue refresh wait on a foreign node's lock.

A second process takes only the POSIX record lock on each lock file, which is
the half GPFS holds across nodes. A hold recorded meanwhile must not reach the
log until that holder releases, and a refresh must read the snapshot on disk
rather than fetch. At the base revision neither site waits on a record lock, so
both cases fail there: the hold is written while the holder holds, and the
refresh fetches.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import model_catalogue
from reckon.crew.picker import outcomes
from tests.cross_node_lock_holder import record_lock_held_elsewhere


def _log_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_a_hold_waits_for_a_record_lock_holder_on_another_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    log_path = outcomes._hold_path("sample")
    lock_path = log_path.with_name(log_path.name + ".lock")

    written: list[dict[str, Any]] = []

    def record() -> None:
        written.append(
            outcomes.record_picker_hold(
                project="sample",
                node="probe-node",
                plan="sample-plan",
                selection={
                    "rendered_state": "state",
                    "questions": {},
                    "confidence": 0.5,
                },
                reason="probe",
            )
        )

    with record_lock_held_elsewhere(lock_path) as holder:
        assert holder.poll() is None, "the holder exited early"
        worker = threading.Thread(target=record, daemon=True)
        worker.start()
        # The hold must not reach the log while the record lock is held.
        time.sleep(0.6)
        assert worker.is_alive(), "the hold did not wait for the record-lock holder"
        assert _log_lines(log_path) == [], (
            "the hold was written while the lock was held"
        )

    worker.join(timeout=60)
    assert not worker.is_alive(), "the hold never proceeded after the holder released"
    lines = _log_lines(log_path)
    assert len(lines) == 1, f"the released hold is not one whole line: {lines}"
    record_on_disk = json.loads(lines[0])
    assert record_on_disk["node"] == "probe-node"
    assert written and written[0]["held_at"] == record_on_disk["held_at"]


def _write_snapshot(models: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot = {
        "checked_at": "2020-01-01T00:00:00Z",
        "list_checked_at": "2020-01-01T00:00:00Z",
        "models": models,
        "notices": [],
    }
    path = model_catalogue.snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return snapshot


def test_a_refresh_reads_the_snapshot_while_a_record_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    models = [{"id": "model-one", "slug": "model-one"}]
    snapshot = _write_snapshot(models)
    config = {"model_catalogue": {"list_url": "https://lists.invalid/models"}}
    fetches: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        fetches.append(url)
        return {"data": [{"id": "model-two", "slug": "model-two"}]}

    lock_path = model_catalogue._root() / "refresh.lock"

    with record_lock_held_elsewhere(lock_path):
        during = model_catalogue.check_model_catalogue(
            "sample", config=config, fetch=fetch, force=True
        )

    assert fetches == [], "the refresh fetched while a record-lock holder held"
    assert during == snapshot

    # Control: with the lock free the same call does fetch, so the fetch that
    # the held call skipped is the one this test can see happen.
    after = model_catalogue.check_model_catalogue(
        "sample", config=config, fetch=fetch, force=True
    )
    assert fetches == ["https://lists.invalid/models"]
    assert after["models"] == [{"id": "model-two", "slug": "model-two"}]
