"""Torn run documents are reported; ledger writers publish complete files."""

from __future__ import annotations

import json
from pathlib import Path

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


@pytest.mark.parametrize("writer", ["append", "split"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_run_publication_is_atomic(store, monkeypatch, writer, interrupted):
    root, records = store
    row = {"run_id": "r-published", "outcome": "complete payload"}
    target = records / "r-published.json"
    if writer == "split":
        ledger.ledger_path("sample", root).write_text(
            json.dumps({"data": {"runs": [row], "members": [], "holds": []}})
        )
        monkeypatch.setattr(
            ledger, "_state_checkout", lambda *a: (root, Path("crew.json"))
        )
        monkeypatch.setattr(ledger, "_commit_run_split", lambda *a, **k: "fixture")
    original_open = Path.open
    observations = []

    class InterruptedWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, text):
            midpoint = len(text) // 2
            self.handle.write(text[:midpoint])
            self.handle.flush()
            observations.append(target.exists())
            assert not target.exists(), "reader can see an incomplete run document"
            data, _version = ledger.load("sample", root, use_index=False)
            assert {r["run_id"] for r in data["runs"]} >= {"r-first", "r-last"}
            assert ledger.damaged_run_records("sample") == ()
            if interrupted:
                raise OSError("interrupted run write")
            self.handle.write(text[midpoint:])
            return len(text)

    def observed_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if records in path.parents and any(flag in mode for flag in ("w", "x")):
            return InterruptedWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", observed_open)
    original_sibling = ledger._store._open_sibling_temporary

    def observed_sibling(path, *args, **kwargs):
        handle, temporary = original_sibling(path, *args, **kwargs)
        if records in path.parents:
            handle = InterruptedWriter(handle)
        return handle, temporary

    monkeypatch.setattr(ledger._store, "_open_sibling_temporary", observed_sibling)

    def publish():
        if writer == "append":
            return ledger.append_run("sample", row, root=root)
        return ledger._split_project_runs("sample", root / "docs", dry_run=False)

    if interrupted:
        with pytest.raises(OSError, match="interrupted run write"):
            publish()
        assert not target.exists()
    else:
        publish()
        assert json.loads(target.read_text()) == row
    assert observations == [False]
    assert sorted(p.name for p in records.iterdir()) == (
        ["r-first.json", "r-last.json"]
        if interrupted
        else ["r-first.json", "r-last.json", "r-published.json"]
    )


def test_duplicate_arriving_at_publication_is_preserved(store, monkeypatch):
    root, records = store
    target = records / "r-racing.json"
    competing = {"run_id": "r-racing", "outcome": "winner"}
    original = ledger._store.write_atomically

    def competing_write(path, render, **kwargs):
        original(path, render, **kwargs)
        target.write_text(ledger.serialize_run(competing))

    monkeypatch.setattr(ledger._store, "write_atomically", competing_write)
    with pytest.raises(ledger.LedgerError, match="already exists"):
        ledger.append_run("sample", {"run_id": "r-racing"}, root=root)
    assert json.loads(target.read_text()) == competing
    assert not any(p.is_dir() for p in records.iterdir())
