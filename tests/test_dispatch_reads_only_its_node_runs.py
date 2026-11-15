"""Dispatch numbers an attempt from its own node's rows, not the whole ledger.

The admission service sends every queued launch through dispatch, and decoding
the whole ledger there cost minutes per launch while the lane ran 4 of 12. The
attempt needs only this node's earlier rows, which the run index answers with
one filtered query.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from reckon import ledger
from reckon.crew.dispatching import preparation


def _index(path: Path, aggregate: list[dict], records: list[dict]) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE aggregate_rows (run_id TEXT, payload TEXT)")
        connection.execute("CREATE TABLE records (name TEXT, payload TEXT)")
        connection.executemany(
            "INSERT INTO aggregate_rows VALUES (?, ?)",
            [(row["run_id"], json.dumps(row)) for row in aggregate],
        )
        connection.executemany(
            "INSERT INTO records VALUES (?, ?)",
            [(row["run_id"], json.dumps(row)) for row in records],
        )
    return path


def test_only_the_nodes_rows_are_read_in_completion_order(
    tmp_path, monkeypatch
) -> None:
    path = _index(
        tmp_path / "index.sqlite",
        aggregate=[
            {
                "run_id": "r-2",
                "node": "n",
                "completed_at": "2026-11-14T02:00:00Z",
                "attempt": 2,
            },
            {"run_id": "r-x", "node": "other", "completed_at": "2026-11-14T03:00:00Z"},
        ],
        records=[
            {
                "run_id": "r-1",
                "node": "n",
                "completed_at": "2026-11-14T01:00:00Z",
                "attempt": 1,
            },
            {
                "run_id": "r-2",
                "node": "n",
                "completed_at": "2026-11-14T02:00:00Z",
                "attempt": 2,
            },
        ],
    )
    monkeypatch.setattr(ledger, "_run_index_path", lambda project, root: path)

    def no_full_read(*_args, **_kwargs):
        raise AssertionError("the whole ledger was read")

    monkeypatch.setattr(ledger, "runs", no_full_read)

    rows = preparation._committed_node_runs("p", None, "n")

    assert [row["run_id"] for row in rows] == ["r-1", "r-2"]


def test_an_unreadable_index_falls_back_to_the_ledger(tmp_path, monkeypatch) -> None:
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

    rows = preparation._committed_node_runs("p", None, "n")

    assert [row["run_id"] for row in rows] == ["r-9"]
