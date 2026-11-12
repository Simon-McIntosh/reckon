"""A node lookup uses the selective index while retaining project scoping."""

import json
import sqlite3

import pytest

from reckon import run_store


@pytest.fixture
def populated_stores(tmp_path, monkeypatch):
    hot = tmp_path / "run_store.db"
    monkeypatch.setenv("RECKON_RUN_STORE", str(hot))
    expected = []
    for path in (hot, hot.with_name("run_store.archive.db")):
        records = [
            {"run_id": f"{path.stem}-{i}", "node": f"node-{i}", "project": "sample"}
            for i in range(200)
        ]
        records.append(
            {"run_id": f"{path.stem}-other", "node": "node-0", "project": "other"}
        )
        with sqlite3.connect(path) as connection:
            connection.executescript(run_store._RUNS_SCHEMA)
            connection.executemany(
                "INSERT INTO runs (run_id, node, project, payload) VALUES (?, ?, ?, ?)",
                [
                    (r["run_id"], r["node"], r["project"], json.dumps(r))
                    for r in records
                ],
            )
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM runs WHERE node = ? "
                "AND project = ?",
                ("node-0", "sample"),
            ).fetchall()
            assert "idx_runs_project" in str(plan), plan
        expected.extend(r for r in records if r["node"] == "node-0")
    return expected


@pytest.mark.parametrize("project", ["sample", None])
def test_node_lookup_uses_node_index(populated_stores, monkeypatch, project):
    plans = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def trace(statement):
            if statement.startswith('SELECT "payload" FROM "runs"'):
                plans.append(
                    connection.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
                )

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(run_store.sqlite3, "connect", traced_connect)
    actual = run_store.runs_for_node("node-0", project=project)
    expected = [
        r for r in populated_stores if project is None or r["project"] == project
    ]
    assert sorted(actual, key=lambda r: r["run_id"]) == sorted(
        expected, key=lambda r: r["run_id"]
    )
    assert len(plans) == 2, plans
    print(f"project={project!r}; executed query plans: {plans}")
    assert all("idx_runs_node" in str(plan) for plan in plans), plans
