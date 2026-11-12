"""Torn run documents are reported while exclusive appends retain their guard."""

from __future__ import annotations

import json

import pytest

from reckon import ledger
from reckon.crew import recovery_review_acceptance as acceptance
from tests.test_a_review_run_promotes_itself import PROJECT
from tests.test_a_review_run_promotes_itself import (
    repository as repository,  # noqa: PLC0414
)
from tests.test_one_run_cannot_stop_a_sweep import (
    review_runs as review_runs,  # noqa: PLC0414
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    state = root / "docs" / "state" / "sample"
    records = state / "runs"
    records.mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (state / "crew.json").write_text(
        json.dumps({"data": {"members": [], "runs": [], "holds": []}})
    )
    for run_id in ("r-first", "r-last"):
        (records / f"{run_id}.json").write_text(json.dumps({"run_id": run_id}))
    return root, records


@pytest.mark.parametrize("use_index", [False, True])
def test_truncated_row_is_reported_and_repaired_row_is_seen(store, caplog, use_index):
    root, records = store
    damaged = records / "r-damaged.json"
    damaged.write_text('{"run_id":"r-damaged"')
    for _ in range(2):
        data, _version = ledger.load("sample", root, use_index=use_index)
        assert {row["run_id"] for row in data["runs"]} == {"r-first", "r-last"}
        assert ledger.damaged_run_records("sample") == (damaged,)
        assert str(damaged) in caplog.text
    ledger.write_run_file("sample", {"run_id": "r-damaged"}, root)
    data, _version = ledger.load("sample", root, use_index=use_index)
    assert {row["run_id"] for row in data["runs"]} == {"r-first", "r-last", "r-damaged"}
    assert ledger.damaged_run_records("sample") == ()


def test_review_pass_keeps_promoting_with_a_truncated_row(repository, review_runs):
    damaged = ledger.run_path(PROJECT, "r-damaged", repository)
    damaged.parent.mkdir(parents=True, exist_ok=True)
    damaged.write_text('{"run_id":"r-damaged"')
    report = acceptance.dispatch_awaiting_reviews(
        project=PROJECT, session="session-fixture", pointers=review_runs
    )
    assert report["failed"] == []
    assert report["self_promoted"] == [row["run_id"] for row in review_runs]
    data, _version = ledger.load(PROJECT, repository)
    assert {row["run_id"] for row in data["runs"]} == {
        row["run_id"] for row in review_runs
    }
    assert ledger.damaged_run_records(PROJECT) == (damaged,)


def test_duplicate_append_is_refused_without_changing_its_record(store):
    root, records = store
    row = {"run_id": "r-duplicate", "outcome": "original record"}
    ledger.append_run("sample", row, root=root)
    target = records / "r-duplicate.json"
    original = target.read_bytes()
    with pytest.raises(ledger.LedgerError, match="already exists"):
        ledger.append_run(
            "sample", {"run_id": "r-duplicate", "outcome": "replacement"}, root=root
        )
    assert target.read_bytes() == original
    assert json.loads(original) == row


@pytest.mark.parametrize("use_index", [False, True])
def test_read_during_exclusive_append_reports_damage_then_recovers(
    store, monkeypatch, caplog, use_index
):
    from pathlib import Path

    root, records = store
    row = {"run_id": "r-appending", "outcome": "complete record"}
    target = records / "r-appending.json"
    original_open = Path.open
    observations = []

    class AppendingWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, text):
            midpoint = len(text) // 2
            self.handle.write(text[:midpoint])
            self.handle.flush()
            data, _version = ledger.load("sample", root, use_index=use_index)
            observations.append({r["run_id"] for r in data["runs"]})
            assert observations[-1] == {"r-first", "r-last"}
            assert ledger.damaged_run_records("sample") == (target,)
            assert str(target) in caplog.text
            self.handle.write(text[midpoint:])
            return len(text)

    def observed_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return AppendingWriter(handle) if path == target and mode == "x" else handle

    monkeypatch.setattr(Path, "open", observed_open)
    ledger.append_run("sample", row, root=root)
    assert observations == [{"r-first", "r-last"}]
    data, _version = ledger.load("sample", root, use_index=use_index)
    assert {r["run_id"] for r in data["runs"]} == {"r-first", "r-last", "r-appending"}
    assert ledger.damaged_run_records("sample") == ()
    assert json.loads(target.read_text()) == row
