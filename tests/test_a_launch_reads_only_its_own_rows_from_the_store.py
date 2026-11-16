"""A launch reads its node's and its plan's rows from the run store, not the history.

Dispatch numbers an attempt from the node's earlier rows and routes a section by
how many attempts it has had. Where a run store holds the history, both reads
select their rows inside the store, so the cost of a launch does not grow with
the number of runs the project has ever recorded.
"""

from __future__ import annotations

import json

import pytest

from reckon import ledger, mcp_views, run_store
from reckon.crew.dispatching import preparation

PROJECT = "example"
OTHER_ROWS = 200


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("RECKON_RUN_STORE", raising=False)
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    wanted = [
        {
            "run_id": f"wanted-{index}",
            "project": PROJECT,
            "node": "wanted-node",
            "plan": "selected",
            "section": "work",
            "role": "implement",
            "gate": "passed",
            "dispatched_at": f"2026-01-01T0{index}:00:00Z",
            "completed_at": f"2026-01-01T0{2 - index}:30:00Z",
        }
        for index in range(2)
    ]
    others = [
        {
            "run_id": f"other-{index:04d}",
            "project": PROJECT,
            "node": f"other-node-{index}",
            "plan": "unselected",
            "section": "work",
            "role": "implement",
            "gate": "passed",
            "completed_at": "2026-01-01T03:00:00Z",
            "outcome": "other-history-payload",
        }
        for index in range(OTHER_ROWS)
    ]
    with run_store.RunStore() as opened:
        for row in [*others, *wanted]:
            opened.append(PROJECT, row)
    return wanted


@pytest.fixture
def decoded_history(monkeypatch):
    """Count the payloads of other nodes and plans that a read decodes."""
    original = json.loads
    decoded: list[str] = []

    def counted(text, *args, **kwargs):
        if isinstance(text, str) and "other-history-payload" in text:
            decoded.append(text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", counted)
    counted(json.dumps({"outcome": "other-history-payload"}))
    assert len(decoded) == 1, "the decode counter missed its positive control"
    decoded.clear()
    return decoded


def _refuse(name):
    def refused(*_args, **_kwargs):
        raise AssertionError(f"{name} read the whole history")

    return refused


def test_a_nodes_rows_come_from_the_store_in_completion_order(
    store, decoded_history, monkeypatch
) -> None:
    monkeypatch.setattr(ledger, "runs", _refuse("ledger.runs"))

    rows = preparation._committed_node_runs(PROJECT, None, "wanted-node")

    assert [row["run_id"] for row in rows] == ["wanted-1", "wanted-0"]
    assert decoded_history == []


def test_a_node_with_no_stored_row_reads_nothing(
    store, decoded_history, monkeypatch
) -> None:
    monkeypatch.setattr(ledger, "runs", _refuse("ledger.runs"))

    assert preparation._committed_node_runs(PROJECT, None, "never-ran") == []
    assert decoded_history == []


def test_a_sections_attempts_are_counted_from_its_plans_rows(
    store, decoded_history, monkeypatch
) -> None:
    monkeypatch.setattr(ledger, "indexed_headers", _refuse("indexed_headers"))
    monkeypatch.setattr(ledger, "picker_runs", _refuse("picker_runs"))

    assert mcp_views.section_attempt_count(PROJECT, "selected", "work") == 2
    assert mcp_views.section_attempt_count(PROJECT, "selected", "absent") == 0
    assert decoded_history == []


def test_a_project_without_a_store_still_asks_its_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RECKON_RUN_STORE", raising=False)
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        ledger,
        "_run_index_path",
        lambda project, root: tmp_path / "missing" / "index.sqlite",
    )
    monkeypatch.setattr(
        ledger,
        "runs",
        lambda project, root=None: [
            {"run_id": "r-9", "node": "n"},
            {"run_id": "r-8", "node": "z"},
        ],
    )

    rows = preparation._committed_node_runs(PROJECT, None, "n")

    assert [row["run_id"] for row in rows] == ["r-9"]
