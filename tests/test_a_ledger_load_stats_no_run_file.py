"""Ledger reads select stored payloads before decoding and never stat run files."""

import json
import os
from pathlib import Path

import pytest

from reckon import ledger, run_store


@pytest.fixture
def population(tmp_path, monkeypatch):
    monkeypatch.delenv("RECKON_RUN_STORE", raising=False)
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    project = "example"
    directory = ledger.ledger_path(project).parent / "runs"
    directory.mkdir(parents=True)
    rows = [
        {
            "run_id": f"run-{index:04d}",
            "project": project,
            "plan": "selected" if index == 0 else "unselected",
            "dispatched_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T01:00:00Z",
            "wall_seconds": 1,
            "outcome": "unselected-full-payload" if index else "selected-payload",
            "node_definition": {"padding": "x" * 4096},
        }
        for index in range(200)
    ]
    for row in rows:
        (directory / (row["run_id"] + ".json")).write_text(json.dumps(row))
    with run_store.RunStore() as store:
        for row in rows:
            store.append(project, row)
    return project, directory, rows


def test_a_ledger_load_stats_no_run_file(population, monkeypatch):
    project, directory, rows = population
    original = os.stat
    observed = []

    def counted(path, *args, **kwargs):
        if (
            isinstance(path, (str, bytes, os.PathLike))
            and Path(path).parent == directory
        ):
            observed.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", counted)
    (directory / (rows[0]["run_id"] + ".json")).stat()
    assert len(observed) == 1, "the stat instrument missed its positive control"
    observed.clear()
    loaded, _ = ledger.load(project)
    assert len(loaded["runs"]) == len(rows) == 200
    assert observed == [], f"ledger.load stat-ed {len(observed)} run files"


@pytest.mark.parametrize("figures", [False, True])
def test_plan_reads_decode_only_selected_payloads(population, monkeypatch, figures):
    project, _, _ = population
    original = json.loads
    decoded = []

    class CountedJSON:
        def __getattr__(self, name):
            return getattr(json, name)

        def loads(self, value, *args, **kwargs):
            if "unselected-full-payload" in str(value):
                decoded.append(value)
            return original(value, *args, **kwargs)

    monkeypatch.setattr(run_store, "json", CountedJSON())
    run_store.json.loads('{"outcome": "unselected-full-payload"}')
    assert len(decoded) == 1, "the decode instrument missed its positive control"
    decoded.clear()
    for _ in range(3):
        records, _ = ledger.read_records(project, plan="selected", with_figures=figures)
        assert [row["run_id"] for row in records] == ["run-0000"]
        if figures:
            assert records[0]["width_at_start"] == 200
            assert records[0]["wall_seconds"] == 3600
    assert decoded == [], f"decoded {len(decoded)} unselected full payloads"


def test_headers_decode_no_full_payloads(population, monkeypatch):
    project, _, rows = population
    original = json.loads

    class HeadersJSON:
        def __getattr__(self, name):
            return getattr(json, name)

        def loads(self, value, *args, **kwargs):
            assert "-payload" not in str(value), "headers decoded a full payload"
            return original(value, *args, **kwargs)

    monkeypatch.setattr(run_store, "json", HeadersJSON())
    loaded, _ = ledger.load(project, headers_only=True)
    assert loaded["runs"] == [{"run_id": row["run_id"]} for row in rows]
