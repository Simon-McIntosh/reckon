"""Incremental ledger reads preserve picker inputs as history changes."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import budget, capabilities, ledger
from reckon.crew.node import TaskNode
from reckon.crew.picker import lane_context, prompts, snapshot

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _row(name, wall=10):
    return {
        "run_id": name,
        "node": "work",
        "plan": "example",
        "backend": "worker",
        "agent": {"model": "model", "effort": "high"},
        "role": "implement",
        "spec_level": "guided",
        "gate": "passed",
        "completed_at": NOW.isoformat(),
        "completed_at_source": "provided",
        "time_budget": "20m",
        "wall_seconds": wall,
        "budget": {"utilisation_pct": wall, "threshold_status": "allowed"},
    }


def _fixture(tmp_path, monkeypatch, *, aggregate_rows=()):
    monkeypatch.delenv("RECKON_PICK_CACHE", raising=False)
    root = tmp_path / "repo"
    aggregate = ledger.ledger_path("sample", root)
    aggregate.parent.mkdir(parents=True)
    aggregate.write_text(
        json.dumps(
            {
                "data": {
                    "_version": 4,
                    "members": [],
                    "holds": [],
                    "runs": list(aggregate_rows),
                }
            }
        )
    )
    directory = aggregate.parent / "runs"
    directory.mkdir()
    return root, aggregate, directory


def _write(directory, row):
    path = directory / f"{row['run_id']}.json"
    path.write_text(json.dumps(row))
    return path


def test_append_reads_only_the_new_run(tmp_path, monkeypatch):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    for i in range(40):
        _write(directory, _row(f"run-{i}"))
    reads = []
    original = ledger._read_run

    def read(path):
        reads.append(path.name)
        return original(path)

    monkeypatch.setattr(ledger, "_read_run", read)
    assert len(ledger.runs("sample", root)) == 40
    assert len(reads) == 40  # Positive control: the instrument sees actual reads.
    reads.clear()
    _write(directory, _row("appended"))
    assert len(ledger.runs("sample", root)) == 41
    assert reads == ["appended.json"]
    reads.clear()
    ledger.load("sample", root)
    assert reads == []


def test_fresh_process_reuses_index_after_append(tmp_path, monkeypatch):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    _write(directory, _row("first"))
    ledger.load("sample", root)
    _write(directory, _row("second"))
    script = """
import json, sys
from reckon import ledger
reads=[]
original=ledger._read_run
def read(path):
    reads.append(path.name)
    return original(path)
ledger._read_run=read
rows=ledger.runs('sample',sys.argv[1])
print(json.dumps({'reads':reads,'ids':[row['run_id'] for row in rows]}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        env={**os.environ, "PYTHONPATH": str(Path(ledger.__file__).parent.parent)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "reads": ["second.json"],
        "ids": ["first", "second"],
    }


def test_index_matches_uncached_union_after_every_mutation(tmp_path, monkeypatch):
    first = _row("first")
    root, aggregate, directory = _fixture(tmp_path, monkeypatch, aggregate_rows=[first])
    _write(directory, first)

    def same():
        assert ledger.load("sample", root) == ledger.load(
            "sample", root, use_index=False
        )
        headers, version = ledger.indexed_headers("sample", root)
        full, full_version = ledger.load("sample", root, use_index=False)
        assert version == full_version
        assert ledger.history_version(headers, version) == ledger.history_version(
            full, full_version
        )

    same()
    extra = _write(directory, _row("second"))
    same()
    _write(directory, _row("second", wall=90))
    same()
    extra.unlink()
    same()
    aggregate.write_text(
        json.dumps(
            {
                "data": {
                    "_version": 5,
                    "members": [{"id": "person"}],
                    "holds": [],
                    "runs": [],
                }
            }
        )
    )
    same()


def test_conflicting_history_refuses_even_after_index_warmup(tmp_path, monkeypatch):
    first = _row("first")
    root, _aggregate, directory = _fixture(
        tmp_path, monkeypatch, aggregate_rows=[first]
    )
    _write(directory, first)
    ledger.load("sample", root)
    _write(directory, _row("first", wall=80))
    with pytest.raises(
        ledger.LedgerError, match="refusing to read conflicting history"
    ):
        ledger.load("sample", root)


@pytest.mark.parametrize("damage", ["database", "header", "payload"])
def test_corrupt_index_rebuilds(tmp_path, monkeypatch, damage):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    _write(directory, _row("first"))
    expected = ledger.load("sample", root)
    cache = ledger._run_index_path("sample", root)
    if damage == "database":
        cache.write_bytes(b"broken database")
    else:
        with sqlite3.connect(cache) as connection:
            if damage == "header":
                connection.execute(
                    "UPDATE metadata SET payload='broken' WHERE name='header'"
                )
            else:
                connection.execute("UPDATE records SET payload='broken'")
    assert ledger.load("sample", root) == expected


def test_index_failure_preserves_uncached_read(tmp_path, monkeypatch):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    _write(directory, _row("first"))
    monkeypatch.setattr(
        ledger,
        "_indexed_data",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert ledger.load("sample", root) == ledger.load("sample", root, use_index=False)


def test_in_place_edit_with_preserved_mtime_is_visible(tmp_path, monkeypatch):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    path = _write(directory, _row("first", wall=10))
    initial = ledger.input_stamp("sample", root)
    ledger.load("sample", root)
    before = path.stat()
    _write(directory, _row("first", wall=20))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert ledger.input_stamp("sample", root) != initial
    assert ledger.runs("sample", root)[0]["wall_seconds"] == 20


def test_picker_inputs_and_rendered_state_equal_full_build(tmp_path, monkeypatch):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(budget.crew, "list_live", list)
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(tmp_path / "absent-lane.json"))
    monkeypatch.setattr(lane_context, "list_live", list)
    monkeypatch.setattr(
        lane_context, "local_lane_load", lambda: {"read_at": NOW.isoformat()}
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(snapshot, "datetime", Clock)
    config = {
        "default_backend": "worker",
        "backends": {"worker": {"model": "model", "budget_check": False}},
    }
    monkeypatch.setattr(
        capabilities,
        "load_capabilities",
        lambda: {"ledger_versions": {"sample": "obsolete"}},
    )
    dispatch = importlib.import_module("reckon.crew.dispatch")
    node = TaskNode(
        id="work",
        goal="Measure",
        plan="example",
        role="implement",
        spec_level="guided",
        done_when="test",
        time_budget="20m",
    )
    original_load = ledger.load
    original_build = lane_context.build

    def dated_build(**kwargs):
        return original_build(**{**kwargs, "now": NOW})

    monkeypatch.setattr(lane_context, "build", dated_build)
    for row in [_row("first"), _row("second"), _row("second", wall=70)]:
        _write(directory, row)
        indexed = dispatch.build_picker_inputs("sample", config, root, ledger_root=root)
        with monkeypatch.context() as patch:
            patch.setattr(
                ledger,
                "load",
                lambda project, root=None: original_load(
                    project, root, use_index=False
                ),
            )
            patch.setattr(
                ledger,
                "indexed_headers",
                lambda project, root=None: original_load(
                    project, root, use_index=False
                ),
            )
            full = dispatch.build_picker_inputs(
                "sample", config, root, ledger_root=root
            )
        assert indexed[3] == full[3] == {}
        assert indexed[0] == full[0]
        assert indexed[1] == full[1]
        assert indexed[2] == full[2]
        # Budget ages are evaluated at the current instant; fix the time in
        # the public preflight call below so equality includes every clock.
        views = [
            budget.preflight(
                "sample",
                config,
                root=root,
                records=rows,
                windows={},
                now=NOW,
                probe_runner=lambda _: {},
            )
            for rows in (indexed[0], full[0])
        ]
        assert views[0] == views[1]
        rendered = [
            prompts.render(
                "state.jinja",
                node=node,
                capability={},
                estimated_context=None,
                comment="",
                candidates=[],
                project="sample",
                records=rows,
                budget_snapshot=view,
                config=config,
                attempts=0,
            )
            for rows, view in zip((indexed[0], full[0]), views, strict=True)
        ]
        assert rendered[0] == rendered[1]
