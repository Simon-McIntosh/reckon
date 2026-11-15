"""A run store that refuses a run leaves no row in either store.

The run store is the durable authority for a promoted run, so a store write
that fails must refuse the promotion rather than leave a run file with no
durable row behind it. This is the refusal the failure is injected into: the
per-run file a promotion writes before the store write is taken back when the
store raises, so neither store keeps the row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger, run_store

PROJECT = "proj"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the crew config home the run store resolves through."""
    config_home = tmp_path / "config"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A checkout holding an empty aggregate ledger for this project."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    ledger.ledger_path(PROJECT, root).write_text(
        json.dumps({"version": 0, "data": {"runs": [], "members": [], "holds": []}}),
        encoding="utf-8",
    )
    return root


def _record(run_id: str, node: str) -> dict:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "node": node,
        "completed_at": "2026-11-15T10:00:00Z",
        "outcome": "the delivered change is recorded",
    }


ZERO = {
    "rows_in_file_absent_from_store": 0,
    "rows_in_store_absent_from_file": 0,
    "rows_present_in_both_disagreeing": 0,
}


def test_a_refused_store_write_leaves_no_row_in_either_store(
    home: Path, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-store-refused"
    node = "node-refused"
    target = ledger.run_path(PROJECT, run_id, repository)

    def refuse(_project: str, _record: dict) -> None:
        raise RuntimeError("injected store failure")

    monkeypatch.setattr(run_store, "append", refuse)

    with pytest.raises(ledger.LedgerError) as caught:
        ledger.append_run(PROJECT, _record(run_id, node), root=repository)

    message = str(caught.value)
    assert str(run_store.store_path()) in message, message
    assert "scripts/import_runs_into_store.py" in message, message
    assert f"--project {PROJECT}" in message, message

    assert not target.exists(), "the run file must be taken back with the refusal"
    assert run_store.runs_for_node(node, project=PROJECT) == [], "store row leaked"
    comparison = run_store.compare(PROJECT, root=repository)
    assert {key: comparison[key] for key in ZERO} == ZERO, comparison


def test_a_healthy_store_write_records_both_stores_identically(
    home: Path, repository: Path
) -> None:
    run_id = "r-store-written"
    node = "node-written"
    record = _record(run_id, node)

    written = ledger.append_run(PROJECT, record, root=repository)

    assert written["store"] == {"status": "written"}
    target = ledger.run_path(PROJECT, run_id, repository)
    assert target.is_file(), "the run file must survive a store that accepted it"
    rows = run_store.runs_for_node(node, project=PROJECT)
    assert [row["run_id"] for row in rows] == [run_id]
    comparison = run_store.compare(PROJECT, root=repository)
    assert {key: comparison[key] for key in ZERO} == ZERO, comparison
