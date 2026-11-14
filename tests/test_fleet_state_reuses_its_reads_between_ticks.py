"""Fleet refreshes reuse held records while worker streams keep growing."""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reckon import file_memo, ledger
from reckon.crew import obligation_snapshot, review, routing, runs
from tests.helpers.git import git

obligations = importlib.import_module("reckon.crew.obligations")
PROJECT = "refresh-fixture"
SESSION = "coordinator"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repository = tmp_path / "repository"
    docs = repository / "docs"
    (docs / "state" / PROJECT / "runs").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(docs)}))
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
    ):
        git(repository, *args)
    (repository / "seed").write_text("seed")
    git(repository, "add", "seed")
    git(repository, "commit", "-qm", "test: seed fleet\n\nSupply fixture worktrees.")
    head = git(repository, "rev-parse", "HEAD").strip()
    trees = []
    for index in range(4):
        tree = tmp_path / "trees" / SESSION / f"held-{index}"
        tree.parent.mkdir(parents=True, exist_ok=True)
        git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
        trees.append(tree)
        ledger.write_run_file(
            PROJECT,
            ledger.build_record(
                run_id=f"held-run-{index}",
                plan="fixture",
                node=tree.name,
                session=SESSION,
                completed_at="2026-10-01T12:00:00Z",
                gate="passed",
            ),
            repository,
        )
    streams = []
    created = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    for index in range(60):
        run_id = f"live-{index}"
        directory = runs.runs_dir() / run_id
        directory.mkdir(parents=True)
        stream = directory / "stdout.jsonl"
        stream.write_text('{"type":"thread.started","thread_id":"fixture"}\n')
        if index < 6:
            streams.append(stream)
        pointer = runs.pointer_path(run_id)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "project": PROJECT,
                    "session": SESSION,
                    "repo": str(repository),
                    "worktree": str(repository),
                    "phase": "working",
                    "launcher_host": "fixture-remote-host",
                    "process_alive": True,
                    "created_at": created,
                    "base_sha": head,
                    "log_path": str(stream),
                    "manifest_path": str(directory / "manifest.md"),
                    "node": {"id": run_id, "plan": "fixture"},
                }
            )
        )
        path = (
            review.committed_review_root(PROJECT)
            / review.COMMITTED_RUN_DIRNAME
            / run_id
            / "review.json"
        )
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "project": PROJECT,
                    "reviewed_run_id": run_id,
                    "review_run_id": f"review-{run_id}",
                    "reviewed_head_sha": head,
                    "status": "parsed",
                    "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
                    "absent": [],
                    "total": 18 * len(review.REVIEW_DIMENSIONS),
                }
            )
        )
    git(repository, "add", f"docs/state/{PROJECT}")
    git(
        repository,
        "commit",
        "-qm",
        "test: store fleet records\n\nSupply committed reviews and held records.",
    )
    monkeypatch.setattr(
        obligations.flight, "resolve", lambda *a, **k: SimpleNamespace(config={})
    )
    file_memo.clear()
    return SimpleNamespace(repo=repository, trees=trees, streams=streams)


def patch_bindings(monkeypatch, original, replacement):
    """Patch every loaded spelling of the measured callable."""
    bindings = []
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("reckon."):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, replacement)
                    bindings.append(f"{module.__name__}.{name}")
    assert bindings
    return bindings


def test_stream_growth_reuses_both_reads_and_source_changes_invalidate(
    fleet, monkeypatch
):
    counts = Counter()
    active = False
    held_read = obligations._held_worktree_records

    def held(*args, **kwargs):
        nonlocal active
        active = True
        try:
            return held_read(*args, **kwargs)
        finally:
            active = False

    patch_bindings(monkeypatch, held_read, held)
    scandir = os.scandir

    def listing(*args, **kwargs):
        if active:
            counts["held_listing"] += 1
        return scandir(*args, **kwargs)

    monkeypatch.setattr(os, "scandir", listing)
    bindings = {}
    for label, original in (
        ("held_records", obligations._held_worktree_records_read),
        ("registry_listing", routing._registered_worktrees),
        ("ledger_load", ledger.load),
    ):

        def counted(*args, _label=label, _original=original, **kwargs):
            counts[_label] += 1
            return _original(*args, **kwargs)

        bindings[label] = patch_bindings(monkeypatch, original, counted)

    def refresh():
        state = obligation_snapshot.fleet_state(PROJECT)
        assert len(state.pointers) == 60
        return {row["run_id"] for row in state.held_worktrees.get(SESSION, [])}

    expected = {f"held-run-{index}" for index in range(4)}
    assert refresh() == expected
    assert all(counts[label] > 0 for label in bindings), counts
    assert counts["held_listing"] > 0, counts
    first = dict(counts)
    counts.clear()
    before = obligations._held_records_signature(PROJECT, fleet.repo)
    for stream in fleet.streams:
        with stream.open("a") as output:
            output.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"progress"}}\n'
            )
    assert refresh() == expected
    after = obligations._held_records_signature(PROJECT, fleet.repo)
    print(
        json.dumps(
            {
                "first": first,
                "second": dict(counts),
                "bindings": bindings,
                "held_key_changed": before != after,
            }
        )
    )
    assert counts["held_listing"] == 0, counts
    assert counts["held_records"] == 0, counts
    assert counts["registry_listing"] == 0, counts
    assert counts["ledger_load"] == 0, counts

    row = ledger.build_record(
        plan="fixture",
        run_id="new-holder",
        node=fleet.trees[0].name,
        session=SESSION,
        completed_at="2026-11-01T12:00:00Z",
        gate="passed",
    )
    ledger.write_run_file(PROJECT, row, fleet.repo)
    assert refresh() == expected - {"held-run-0"} | {"new-holder"}
    assert counts["held_records"] > 0 and counts["ledger_load"] == 0, counts
    counts.clear()
    fleet.trees[1].rename(fleet.trees[1].with_name("displaced"))
    assert refresh() == expected - {"held-run-0", "held-run-1"} | {"new-holder"}
    assert counts["held_records"] > 0, counts
    fleet.trees[1].with_name("displaced").rename(fleet.trees[1])
    assert refresh() == expected - {"held-run-0"} | {"new-holder"}


def test_unrelated_landing_and_registry_churn_do_only_changed_work(fleet, monkeypatch):
    transient = fleet.trees[0].parent / "transient"
    git(fleet.repo, "worktree", "add", "-q", "--detach", str(transient), "HEAD")
    for index in range(200):
        ledger.write_run_file(
            PROJECT,
            ledger.build_record(
                plan="fixture",
                run_id=f"history-{index}",
                node=f"unrelated-{index}",
                session=SESSION,
                completed_at="2026-09-01T12:00:00Z",
                gate="passed",
            ),
            fleet.repo,
        )
    counts = Counter()
    bindings = {}
    for method in ("replace", "remove"):
        original_method = getattr(routing.WorktreeRecordIndex, method)

        def counted_method(
            self, *args, _method=method, _original=original_method, **kwargs
        ):
            counts["owner_" + _method] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(routing.WorktreeRecordIndex, method, counted_method)
    for label, original in (
        ("ledger_load", ledger.load),
        ("whole_ledger", obligations._run_namings),
        ("registry_listing", routing._registered_worktrees),
        ("record_read", ledger.read_document),
        ("tree_match", obligations._holding_record),
    ):

        def counted(*args, _label=label, _original=original, **kwargs):
            counts[_label] += 1
            return _original(*args, **kwargs)

        bindings[label] = patch_bindings(monkeypatch, original, counted)

    def refresh():
        state = obligation_snapshot.fleet_state(PROJECT)
        assert len(state.pointers) == 60
        return {row["run_id"] for row in state.held_worktrees.get(SESSION, [])}

    expected = {f"held-run-{index}" for index in range(4)}
    assert refresh() == expected
    assert all(counts[label] > 0 for label in bindings), counts
    assert counts["owner_replace"] > 0, counts
    first = dict(counts)
    counts.clear()
    row = ledger.build_record(
        plan="fixture",
        run_id="unrelated-landing",
        node="unrelated-landing",
        session=SESSION,
        completed_at="2026-11-01T12:00:00Z",
        gate="passed",
    )
    ledger.write_run_file(PROJECT, row, fleet.repo)
    git(fleet.repo, "worktree", "remove", str(transient))
    added = transient.with_name("another-live-tree")
    git(fleet.repo, "worktree", "add", "-q", "--detach", str(added), "HEAD")
    for stream in fleet.streams:
        with stream.open("a") as output:
            output.write('{"type":"thread.started","thread_id":"growing"}\n')
    assert refresh() == expected
    print(json.dumps({"first": first, "changed": dict(counts), "bindings": bindings}))
    assert counts["ledger_load"] == 0, counts
    assert counts["whole_ledger"] == 0, counts
    assert counts["registry_listing"] == 0, counts
    assert counts["record_read"] == 1, counts
    assert counts["owner_replace"] == 1, counts
    assert counts["tree_match"] == 1, counts

    counts.clear()
    row["node"] = fleet.trees[0].name
    ledger.write_run_file(PROJECT, row, fleet.repo)
    assert refresh() == expected - {"held-run-0"} | {"unrelated-landing"}
    assert counts["record_read"] == 1 and counts["tree_match"] == 1, counts
    assert counts["ledger_load"] == counts["whole_ledger"] == 0, counts

    counts.clear()
    source = ledger.run_path(PROJECT, "unrelated-landing", fleet.repo)
    source.unlink()
    assert refresh() == expected
    assert counts["owner_remove"] == 1 and counts["owner_replace"] == 0, counts
    assert counts["record_read"] == 0 and counts["tree_match"] == 1, counts

    counts.clear()
    registered = transient.with_name("unrelated-0")
    git(fleet.repo, "worktree", "add", "-q", "--detach", str(registered), "HEAD")
    assert refresh() == expected | {"history-0"}
    assert counts["record_read"] == 0 and counts["tree_match"] == 1, counts
    assert (
        counts["registry_listing"]
        == counts["whole_ledger"]
        == counts["ledger_load"]
        == 0
    ), counts


def test_an_aggregate_rewrite_rebuilds_from_the_current_union(fleet):
    before = obligation_snapshot.fleet_state(PROJECT)
    assert {row["run_id"] for row in before.held_worktrees[SESSION]} == {
        f"held-run-{index}" for index in range(4)
    }
    row = ledger.build_record(
        plan="fixture",
        run_id="aggregate-holder",
        node=fleet.trees[0].name,
        session=SESSION,
        completed_at="2026-11-01T12:00:00Z",
        gate="passed",
    )
    earlier = dict(row, run_id="earlier-holder", completed_at="2026-10-15T12:00:00Z")
    path = ledger.ledger_path(PROJECT, fleet.repo)
    path.write_text(
        json.dumps(
            {
                "data": {
                    "members": [],
                    "runs": [row, earlier],
                    "holds": [],
                    "_version": 1,
                }
            }
        )
    )
    after = obligation_snapshot.fleet_state(PROJECT)
    assert {item["run_id"] for item in after.held_worktrees[SESSION]} == {
        "aggregate-holder",
        "held-run-1",
        "held-run-2",
        "held-run-3",
    }

    for index in range(4):
        ledger.run_path(PROJECT, f"held-run-{index}", fleet.repo).unlink()
    aggregate_only = obligation_snapshot.fleet_state(PROJECT)
    assert {item["run_id"] for item in aggregate_only.held_worktrees[SESSION]} == {
        "earlier-holder"
    }


def test_the_shared_record_index_replaces_and_removes_naming_routes(tmp_path):
    index = routing.WorktreeRecordIndex()
    assert callable(getattr(index, "replace", None)), (
        "the shared owner must support replacement"
    )
    assert callable(getattr(index, "remove", None)), (
        "the shared owner must support removal"
    )
    first_tree = tmp_path / "first-session" / "node"
    other_tree = tmp_path / "other-session" / "node"
    retained_tree = tmp_path / "retained"
    first = {"run_id": "first", "node": "node", "session": "first-session"}
    second = {"run_id": "second", "node": "node", "session": "first-session"}
    index.replace("first", first)
    index.replace("second", second)
    assert index.records_naming(first_tree) == [first, second]
    assert index.records_naming(other_tree) == []
    first = dict(first, gate="passed")
    index.replace("first", first)
    assert index.records_naming(first_tree) == [first, second]

    changed = dict(
        first,
        session="other-session",
        worktree_retention={"worktree": str(retained_tree)},
    )
    affected = index.replace("first", changed)
    assert set(affected) & set(index.worktree_keys(first_tree))
    assert set(affected) & set(index.worktree_keys(other_tree))
    assert index.records_naming(first_tree) == [second]
    assert index.records_naming(other_tree) == [changed]
    assert index.records_naming(retained_tree) == [changed]
    index.remove("first")
    assert index.records_naming(other_tree) == index.records_naming(retained_tree) == []
    assert index.records_naming(first_tree) == [second]
    index.remove("second")
    assert index.records_naming(first_tree) == []
    assert index.remove("absent") == ()

    index.add(first)
    index.add(second)
    assert index.records_naming(first_tree) == [first, second]
