"""Held-tree discovery is reused until a source identity changes."""

from __future__ import annotations

import importlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import file_memo, ledger
from reckon.crew import obligation_snapshot, routing, runs, station_obligations
from tests.helpers.git import git

obligations = importlib.import_module("reckon.crew.obligations")
PROJECT = "held-change-fixture"
SESSION = "held-session"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repository = tmp_path / "repository"
    (repository / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repository / "docs")}))
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "seed").write_text("seed")
    git(repository, "add", "seed")
    git(repository, "commit", "-qm", "test: seed held trees")
    trees = []

    def add_tree():
        index = len(trees)
        tree = tmp_path / "trees" / SESSION / f"held-{index}"
        tree.parent.mkdir(parents=True, exist_ok=True)
        git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
        row = ledger.build_record(
            run_id=f"held-run-{index}",
            plan="fixture-plan",
            node=tree.name,
            session=SESSION,
            gate="passed",
            completed_at="2026-10-01T12:00:00Z",
        )
        ledger.write_run_file(PROJECT, row, repository)
        trees.append(tree)
        return tree

    for _ in range(20):
        add_tree()
    for index in range(30):
        pointer = runs.pointer_path(f"live-run-{index}")
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "run_id": f"live-run-{index}",
                    "project": PROJECT,
                    "session": SESSION,
                    "worktree": str(tmp_path / "live-trees" / str(index)),
                }
            )
        )
    # Exercise the real refresh and fleet-state derivation, isolating duties
    # unrelated to held trees so no classifier or follower process is needed.
    monkeypatch.setattr(
        obligations.flight, "resolve", lambda *_: SimpleNamespace(config={})
    )
    monkeypatch.setattr(obligations, "_current_review_in_flight", lambda *_: False)
    monkeypatch.setattr(obligations, "_classified_rows", lambda *_: [])
    monkeypatch.setattr(obligations, "_acknowledgements_in_force", lambda *a, **k: {})
    monkeypatch.setattr(obligations, "_sub_floor_items_by_session", lambda *a, **k: {})
    monkeypatch.setattr(runs, "drain_unreconciled_by_session", lambda *_: {})
    monkeypatch.setattr(station_obligations, "follower_rows", lambda *_: [])
    monkeypatch.setattr(station_obligations, "_station_producer", lambda *_: {})
    file_memo.clear()
    store = SimpleNamespace(root=str(tmp_path / "station"), obligation_rows=list)
    answers = []
    original_state = obligation_snapshot.fleet_state

    def state(*args, **kwargs):
        result = original_state(*args, **kwargs)
        answers.append(result.held_worktrees.get(SESSION, []))
        return result

    monkeypatch.setattr(obligation_snapshot, "fleet_state", state)

    def refresh():
        station_obligations.refresh(
            store, project=PROJECT, changed=True, now=datetime(2026, 11, 1, tzinfo=UTC)
        )
        return {row["run_id"] for row in answers[-1]}

    return SimpleNamespace(repo=repository, trees=trees, add=add_tree, refresh=refresh)


class Counts:
    """Count discovery aliases and physical work inside the held-tree reader.

    Identity checks belong to cache validation; the counted stats are the
    underlying derivation and the redundant existence checks of held paths.
    Patching os.stat and os.scandir catches pathlib and file_memo as well.
    """

    def __init__(self, monkeypatch, trees):
        self.work = Counter()
        self.active = False
        self.held_active = False
        self.trees = set(trees)
        held_read = obligations._held_worktree_records

        def held(*args, **kwargs):
            self.held_active = True
            try:
                return held_read(*args, **kwargs)
            finally:
                self.held_active = False

        monkeypatch.setattr(obligations, "_held_worktree_records", held)
        read = obligations._held_worktree_records_read

        def measured_read(*args, **kwargs):
            self.work["derive"] += 1
            self.active = True
            try:
                return read(*args, **kwargs)
            finally:
                self.active = False

        monkeypatch.setattr(obligations, "_held_worktree_records_read", measured_read)
        registered = routing._registered_worktrees

        def listing(*args, **kwargs):
            self.work["registry_listing"] += 1
            return registered(*args, **kwargs)

        for module in (routing, obligations):
            monkeypatch.setattr(module, "_registered_worktrees", listing)
        stat, scandir = os.stat, os.scandir

        def measured_stat(path, *args, **kwargs):
            if self.active or (
                isinstance(path, (str, Path)) and Path(path) in self.trees
            ):
                self.work["stat"] += 1
            return stat(path, *args, **kwargs)

        def measured_scandir(*args, **kwargs):
            if self.held_active:
                self.work["listing"] += 1
            return scandir(*args, **kwargs)

        monkeypatch.setattr(os, "stat", measured_stat)
        monkeypatch.setattr(os, "scandir", measured_scandir)
        live = obligations._live_pointers

        def live_listing(*args, **kwargs):
            self.work["live_listing"] += 1
            return live(*args, **kwargs)

        monkeypatch.setattr(obligations, "_live_pointers", live_listing)


def test_an_unchanged_refresh_does_no_held_tree_discovery(fleet, monkeypatch):
    counts = Counts(monkeypatch, fleet.trees)
    expected = {f"held-run-{index}" for index in range(20)}
    assert fleet.refresh() == expected
    assert counts.work["derive"] > 0 and counts.work["stat"] > 0, counts.work
    assert counts.work["registry_listing"] > 0, counts.work
    assert counts.work["live_listing"] > 0 and counts.work["listing"] > 0, counts.work
    counts.work.clear()
    assert fleet.refresh() == expected
    print("unchanged refresh underlying work:", dict(counts.work))
    assert not any(counts.work.values()), counts.work
    added = fleet.add()
    assert fleet.refresh() == expected | {f"held-run-{len(fleet.trees) - 1}"}
    assert added.is_dir()
    assert counts.work["derive"] > 0, counts.work


def test_changed_pointer_and_record_invalidate_the_answer(fleet):
    expected = {f"held-run-{index}" for index in range(20)}
    assert fleet.refresh() == expected
    pointer = runs.pointer_path("live-run-0")
    row = json.loads(pointer.read_text())
    row["worktree"] = str(fleet.trees[0])
    pointer.write_text(json.dumps(row))
    assert fleet.refresh() == expected - {"held-run-0"}
    source = ledger.run_path(PROJECT, "held-run-1", fleet.repo)
    record = ledger.read_document(source)
    record["release"] = {"worktree_released": True}
    # An in-place write leaves the parent directory unchanged.
    source.write_text(ledger.serialize_run(record))
    assert fleet.refresh() == expected - {"held-run-0", "held-run-1"}


def test_a_vanished_directory_invalidates_the_answer(fleet):
    expected = {f"held-run-{index}" for index in range(20)}
    assert fleet.refresh() == expected
    tree = fleet.trees[0]
    tree.rename(tree.with_name("displaced"))
    assert fleet.refresh() == expected - {"held-run-0"}
