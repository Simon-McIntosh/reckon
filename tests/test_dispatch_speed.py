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

import reckon.crew.dispatch_picker as dispatch_picker_module
from reckon import _store, budget, capabilities, ledger
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


def test_ledger_index_shares_cache_root_without_touching_capabilities(
    tmp_path, monkeypatch
):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    cache_path = capabilities.capabilities_path()
    assert not cache_path.exists()

    _write(directory, _row("first"))
    ledger.load("sample", root)
    index_path = ledger._run_index_path("sample", root)
    assert index_path.parent == cache_path.parent
    assert index_path.name.startswith("ledger-")
    assert index_path.suffix == ".sqlite"
    assert set(cache_path.parent.iterdir()) == {index_path}

    cache_value = {"ledger_versions": {"sample": "preserved"}, "configurations": []}
    cache_path.write_text(json.dumps(cache_value))
    before = cache_path.read_bytes()
    _write(directory, _row("second"))
    assert len(ledger.runs("sample", root)) == 2
    ledger.indexed_headers("sample", root)

    assert cache_path.read_bytes() == before
    assert capabilities.load_capabilities() == cache_value
    assert set(cache_path.parent.iterdir()) == {index_path, cache_path}


@pytest.mark.parametrize("existing", [False, True])
def test_index_schema_and_rows_publish_in_one_transaction(
    tmp_path, monkeypatch, existing
):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    _write(directory, _row("first"))
    if existing:
        ledger.picker_runs("sample", root)
        with sqlite3.connect(ledger._run_index_path("sample", root)) as connection:
            connection.execute("PRAGMA user_version=0")

    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    assert ledger.picker_runs("sample", root) == [_row("first")]
    writes = [
        i
        for i, sql in enumerate(statements)
        if sql.startswith(
            ("CREATE ", "DROP ", "INSERT ", "DELETE ", "PRAGMA user_version=")
        )
    ]
    assert writes, statements
    begin = [i for i, sql in enumerate(statements) if sql.startswith("BEGIN")]
    commit = [i for i, sql in enumerate(statements) if sql == "COMMIT"]
    assert len(begin) == len(commit) == 1, statements
    assert begin[0] < min(writes) <= max(writes) < commit[0], statements


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


@pytest.mark.parametrize("reader", ["runs", "picker_runs"])
def test_fresh_process_reuses_index_after_append(tmp_path, monkeypatch, reader):
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
rows=getattr(ledger,sys.argv[2])('sample',sys.argv[1])
print(json.dumps({'reads':reads,'ids':[row['run_id'] for row in rows]}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), reader],
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
    monkeypatch.setattr(budget, "datetime", Clock)
    config = {
        "default_backend": "worker",
        "backends": {
            "worker": {
                "model": "model",
                "budget_check": False,
                "budget_group": "wallet",
            }
        },
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
        row.update(
            {
                "irrelevant_payload": "large prompt omitted from picker inputs",
                "lane_receipt": {
                    "observed_at": NOW.isoformat(),
                    "quota_windows": [
                        {
                            "window_minutes": 10080,
                            "used_percent": row["wall_seconds"],
                            "resets_at": "2026-10-10T12:00:00+00:00",
                        }
                    ],
                },
                "throughput": {"generated_tokens": 21000},
            }
        )
        _write(directory, row)
        indexed = dispatch.build_picker_inputs("sample", config, root, ledger_root=root)
        with monkeypatch.context() as patch:
            patch.setattr(
                capabilities, "cached_pick_input", lambda name, stamp, build: build()
            )
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
            patch.setattr(
                dispatch_picker_module,
                "_picker_ledger_rows",
                lambda project, root: original_load(project, root, use_index=False)[0][
                    "runs"
                ],
            )
            full = dispatch.build_picker_inputs(
                "sample", config, root, ledger_root=root
            )
        assert indexed[3] == full[3] == {}
        assert indexed[0] == [ledger.picker_record(row) for row in full[0]]
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
        from reckon.crew.picker.types import Candidate, PickRequest

        request = PickRequest("sample", node)
        outcomes = [
            snapshot.recent_outcomes(rows, request, "worker", "model", now=NOW)
            for rows in (indexed[0], full[0])
        ]
        assert outcomes[0] == outcomes[1]
        assert outcomes[0]["passed"] == len(indexed[0])
        candidate = Candidate(
            backend="worker",
            family="test",
            model="model",
            effort="high",
            local=False,
            availability="served",
            utilisation_pct=None,
            burn_multiple=None,
            pace_allowance=None,
            resets_at=None,
            worker_slots=None,
            congestion=None,
            outcomes=outcomes[0],
        )
        contexts = [
            lane_context.build(
                node=node,
                candidates=[candidate],
                project="sample",
                records=rows,
                budget_snapshot=view,
                config=config,
            )
            for rows, view in zip((indexed[0], full[0]), views, strict=True)
        ]
        assert contexts[0] == contexts[1]
        assert contexts[0]["return_times"]["worker"]["p50_s"] is not None
        rendered = [
            prompts.render(
                "state.jinja",
                node=node,
                capability={},
                estimated_context=None,
                comment="",
                candidates=[candidate],
                project="sample",
                records=rows,
                budget_snapshot=view,
                config=config,
                attempts=0,
            )
            for rows, view in zip((indexed[0], full[0]), views, strict=True)
        ]
        assert rendered[0] == rendered[1]


def test_candidate_census_reads_the_node_estimate_once(tmp_path, monkeypatch):
    from reckon.crew import routing
    from reckon.crew.picker.types import PickRequest

    calls = []

    def estimate(*args):
        calls.append(args)
        return 3.0, "plan-fallback"

    monkeypatch.setattr(routing, "_estimated_hours", estimate)
    monkeypatch.setattr(routing, "_context_fit_verdict", lambda **kw: None)
    monkeypatch.setattr(
        snapshot, "_serving_observation", lambda backend: {"status": "served"}
    )
    config = {
        "roles": {"implement": {}},
        "backends": {
            f"worker-{i}": {"model": "model", "launch": "cli"} for i in range(8)
        },
    }
    node = TaskNode(
        id="work",
        goal="Measure",
        plan="example",
        role="implement",
        spec_level="guided",
        done_when="test",
        time_budget="20m",
    )
    request = PickRequest("sample", node)
    shared = {"capability_cache": {}, "cache_status": "untracked"}
    view = {"backends": [], "groups": []}
    candidates = snapshot.candidates(
        request,
        config,
        tmp_path,
        records=[],
        verdict_inputs=shared,
        budget_snapshot=view,
        cached_only=True,
    )
    assert len(candidates) == 8
    assert len(calls) == 1
    assert "node_estimate" not in shared


def test_plan_estimate_cache_tracks_edits_and_duplicate_identity(tmp_path, monkeypatch):
    from reckon import resources
    from reckon.crew import routing

    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    plans = root / "docs" / "plans"
    plans.mkdir()
    plan = plans / "example.html"
    plan.write_text(
        '<meta name="reckon-type" content="plan"><meta name="plan-slug" content="example"><meta name="plan-effort-hours" content="2">'
    )
    node = TaskNode(
        id="work",
        goal="Measure",
        plan="example",
        role="implement",
        spec_level="guided",
        done_when="test",
        time_budget="20m",
    )
    calls = []
    resolve = resources.resolve_resource

    def read(*args, **kwargs):
        calls.append(1)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(resources, "resolve_resource", read)
    assert routing._estimated_hours(root, "sample", node) == (2.0, "plan-fallback")
    _write(directory, _row("added"))
    assert routing._estimated_hours(root, "sample", node) == (2.0, "plan-fallback")
    assert len(calls) == 1
    plan.write_text(plan.read_text().replace('content="2"', 'content="3"'))
    assert routing._estimated_hours(root, "sample", node) == (3.0, "plan-fallback")
    (plans / "duplicate.html").write_text(plan.read_text())
    with pytest.raises(resources.ResourceCollision, match="duplicate resource"):
        routing._estimated_hours(root, "sample", node)


@pytest.mark.parametrize("typed_root", ["research", "evidence"])
def test_estimate_rejects_plan_metadata_in_a_different_typed_root(
    tmp_path, monkeypatch, typed_root
):
    from reckon import resources
    from reckon.crew import routing

    root, _aggregate, _directory = _fixture(tmp_path, monkeypatch)
    directory = root / "docs" / typed_root
    directory.mkdir()
    path = directory / "example.html"
    path.write_text(
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="example">'
        '<meta name="plan-effort-hours" content="2">'
    )
    node = TaskNode(
        id="work",
        goal="Measure",
        plan="example",
        role="implement",
        spec_level="guided",
        done_when="test",
        time_budget="20m",
    )
    with pytest.raises(resources.ResourceCollision, match="location type"):
        resources.identify_resource(root / "docs", path, "sample")
    assert (
        resources.resolve_resource(root / "docs", "sample", "example", "plan") is None
    )
    assert routing._estimated_hours(root, "sample", node) == (None, "unavailable")
    # Moving into the untyped compatibility root makes this content resolvable.
    legacy = root / "docs" / "example.html"
    path.rename(legacy)
    assert routing._estimated_hours(root, "sample", node) == (2.0, "plan-fallback")
    legacy.write_text(legacy.read_text().replace('content="2"', 'content="3"'))
    assert routing._estimated_hours(root, "sample", node) == (3.0, "plan-fallback")


def test_permission_error_forfeits_verdict_cache(tmp_path, monkeypatch):
    from reckon.crew import routing

    root, aggregate, directory = _fixture(tmp_path, monkeypatch)
    path = _write(directory, _row("first"))
    reads = []
    monkeypatch.setattr(
        capabilities, "load_capabilities", lambda: reads.append(1) or {}
    )
    monkeypatch.setattr(
        capabilities, "project_cache_status", lambda *a, **kw: "untracked"
    )
    assert routing.shared_verdict_inputs("sample", root)["cache_status"] == "untracked"
    stat = Path.stat

    def denied(self, *args, **kwargs):
        if self in (path, aggregate):
            raise PermissionError("metadata denied")
        return stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    assert ledger._file_identity(path) is None
    for _ in range(2):
        assert (
            routing.shared_verdict_inputs("sample", root)["cache_status"] == "untracked"
        )
    assert len(reads) == 3


def test_profile_stamp_cost_does_not_grow_with_runs(tmp_path, monkeypatch):
    root, aggregate, directory = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(ledger, "ledger_path", lambda *a, **kw: aggregate)
    for i in range(40):
        _write(directory, _row(f"run-{i}"))
    ledger.load("sample", root)
    calls = []
    stat = Path.stat

    def counted(self, *args, **kwargs):
        calls.append(self)
        return stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counted)
    first = lane_context._ledger_stamp("sample")
    assert calls  # The instrument must observe the marker reads.
    assert not any(path.parent == directory for path in calls)
    assert len(calls) <= 12
    calls.clear()
    assert lane_context._ledger_stamp("sample") == first
    assert len(calls) <= 12
    _write(directory, _row("appended"))
    assert lane_context._ledger_stamp("sample") != first
    ledger.load("sample", root)
    before_edit = lane_context._ledger_stamp("sample")
    _write(directory, _row("appended", wall=90))
    ledger.load("sample", root)
    assert lane_context._ledger_stamp("sample") != before_edit


def test_projected_inputs_do_not_decode_retained_full_payloads(tmp_path, monkeypatch):
    dispatch = importlib.import_module("reckon.crew.dispatch")
    full_row = {**_row("aggregate"), "unused_payload": "x" * 4000}
    root, _aggregate, directory = _fixture(
        tmp_path, monkeypatch, aggregate_rows=[full_row]
    )
    for i in range(40):
        _write(directory, {**_row(f"run-{i}"), "unused_payload": "x" * 4000})
    ledger.load("sample", root)
    decodes = []
    original = json.loads

    def decode(value, *args, **kwargs):
        if isinstance(value, str) and '"unused_payload"' in value:
            decodes.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", decode)
    assert len(ledger.runs("sample", root)) == 41
    assert len(decodes) == 41  # Positive control: full payload decoding is visible.
    decodes.clear()
    rows = dispatch._picker_ledger_rows("sample", root)
    assert len(rows) == 41
    assert decodes == []
    _write(directory, {**_row("appended"), "unused_payload": "appended source"})
    rows = dispatch._picker_ledger_rows("sample", root)
    assert len(rows) == 42
    assert len(decodes) == 1
    assert '"appended source"' in decodes[0]
    decodes.clear()
    _write(directory, {**_row("run-0", wall=70), "unused_payload": "edited source"})
    rows = dispatch._picker_ledger_rows("sample", root)
    assert next(row for row in rows if row["run_id"] == "run-0")["wall_seconds"] == 70
    assert len(decodes) == 1
    assert '"edited source"' in decodes[0]
    assert rows == [
        ledger.picker_record(row)
        for row in ledger.load("sample", root, use_index=False)[0]["runs"]
    ]


def test_projected_inputs_refuse_conflicting_full_history(tmp_path, monkeypatch):
    row = {**_row("first"), "unused_payload": "aggregate"}
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch, aggregate_rows=[row])
    _write(directory, row)
    assert ledger.picker_runs("sample", root) == [ledger.picker_record(row)]
    _write(directory, {**row, "unused_payload": "conflict outside projection"})
    with pytest.raises(
        ledger.LedgerError, match="refusing to read conflicting history"
    ):
        ledger.picker_runs("sample", root)


def test_projected_inputs_fall_back_when_index_is_unavailable(tmp_path, monkeypatch):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    _write(directory, {**_row("first"), "unused_payload": "not a picker input"})
    monkeypatch.setattr(
        ledger,
        "_indexed_data",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert ledger.picker_runs("sample", root) == [_row("first")]


def test_profile_reuses_marker_published_during_initial_read(tmp_path, monkeypatch):
    root, aggregate, directory = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(ledger, "ledger_path", lambda *a, **kw: aggregate)
    _write(directory, _row("first"))
    calls = []

    def profile(project, **kwargs):
        calls.append(project)
        ledger.load(project, root)
        return {"groups": []}

    lane_context._PROFILE_CACHE.clear()
    monkeypatch.setattr(lane_context, "run_time_profile", profile)
    lane_context._cached_run_time_profile("sample", now=NOW)
    lane_context._cached_run_time_profile("sample", now=NOW)
    assert calls == ["sample"]


@pytest.mark.parametrize("damage", ["schema", "picker"])
def test_projected_index_recovers_from_incompatible_or_corrupt_cache(
    tmp_path, monkeypatch, damage
):
    root, _aggregate, directory = _fixture(tmp_path, monkeypatch)
    _write(directory, _row("first"))
    expected = ledger.picker_runs("sample", root)
    with sqlite3.connect(ledger._run_index_path("sample", root)) as connection:
        if damage == "schema":
            connection.execute("PRAGMA user_version=0")
        else:
            connection.execute("UPDATE records SET picker='invalid JSON'")
    assert ledger.picker_runs("sample", root) == expected


def test_atomic_run_edit_invalidates_profile_before_index_refresh(
    tmp_path, monkeypatch
):
    root, aggregate, directory = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(ledger, "ledger_path", lambda *a, **kw: aggregate)
    path = _write(directory, _row("first"))
    ledger.load("sample", root)
    first = lane_context._ledger_stamp("sample")
    before = path.read_bytes()
    replacement = json.dumps(_row("first", wall=90))
    rename = os.replace
    attempts = []

    def refuse(source, destination):
        attempts.append((Path(source).read_text(), Path(destination).read_bytes()))
        raise OSError("publication interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", refuse)
        with pytest.raises(OSError, match="publication interrupted"):
            _store.write_atomically(
                path, lambda handle: handle.write(replacement), fsync=False
            )
    assert attempts == [(replacement, before)]
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    assert os.replace is rename
    _store.write_atomically(path, lambda handle: handle.write(replacement), fsync=False)
    assert path.read_text() == replacement
    assert lane_context._ledger_stamp("sample") != first


def test_profile_does_not_cache_across_a_source_change(tmp_path, monkeypatch):
    root, aggregate, directory = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(ledger, "ledger_path", lambda *a, **kw: aggregate)
    _write(directory, _row("first"))
    calls = []

    def profile(project, **kwargs):
        calls.append(project)
        ledger.load(project, root)
        if len(calls) == 1:
            _write(directory, _row("appended"))
        return {"groups": [], "count": len(calls)}

    lane_context._PROFILE_CACHE.clear()
    monkeypatch.setattr(lane_context, "run_time_profile", profile)
    assert lane_context._cached_run_time_profile("sample", now=NOW)["count"] == 1
    assert lane_context._cached_run_time_profile("sample", now=NOW)["count"] == 2
    assert lane_context._cached_run_time_profile("sample", now=NOW)["count"] == 2
    assert calls == ["sample", "sample"]


@pytest.mark.parametrize("with_run", [False, True])
def test_absent_aggregate_retains_verdict_cache(tmp_path, monkeypatch, with_run):
    from reckon.crew import routing

    root, aggregate, directory = _fixture(tmp_path, monkeypatch)
    aggregate.unlink()
    if with_run:
        _write(directory, _row("first"))
    builds = []
    monkeypatch.setattr(capabilities, "load_capabilities", dict)

    def status(*args, **kwargs):
        rows = ledger.runs("sample", root)
        builds.append(rows)
        return "observed"

    monkeypatch.setattr(capabilities, "project_cache_status", status)
    for _ in range(2):
        assert (
            routing.shared_verdict_inputs("sample", root)["cache_status"] == "observed"
        )
    assert len(builds) == 1
    assert len(builds[0]) == int(with_run)
    cache = capabilities.pick_input_cache_path("verdict-inputs-sample")
    assert json.loads(cache.read_text())["value"]["cache_status"] == "observed"
