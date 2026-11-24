"""A held tree whose row lookup failed is offered again on the next refresh."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from reckon import file_memo, ledger
from reckon.crew import (
    obligation_snapshot,
    open_run_index,
    runs,
    station_obligations,
)
from tests.helpers.git import git

obligations = importlib.import_module("reckon.crew.obligations")
PROJECT = "held-retry-fixture"
SESSION = "held-retry-session"


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
    git(
        repository,
        "commit",
        "-qm",
        "test: seed held trees\n\nProvide a committed seed for detached fixture worktrees.",
    )
    trees = []

    def add(index):
        tree = tmp_path / "trees" / SESSION / f"held-{index}"
        tree.parent.mkdir(parents=True, exist_ok=True)
        git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
        # The tree carries a commit its primary branch does not reach, so the
        # derivation holds it: a tree left clean and reachable is dropped before
        # its naming is consulted.
        (tree / "work.txt").write_text(f"{tree.name}\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "-qm", f"test: leave {tree.name} work unmerged\n\nBody.")
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

    for index in range(3):
        add(index)
    # Isolate duties unrelated to held trees so no classifier or follower
    # process is needed for the derivation.
    monkeypatch.setattr(obligations, "_current_review_in_flight", lambda *_: False)
    monkeypatch.setattr(obligations, "_classified_rows", lambda *_: [])
    monkeypatch.setattr(obligations, "_acknowledgements_in_force", lambda *a, **k: {})
    monkeypatch.setattr(obligations, "_sub_floor_items_by_session", lambda *a, **k: {})
    monkeypatch.setattr(
        obligations.flight, "resolve", lambda *_: SimpleNamespace(config={})
    )
    monkeypatch.setattr(runs, "drain_unreconciled_by_session", lambda *a, **k: {})
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

    return SimpleNamespace(repo=repository, trees=trees, add=add, refresh=refresh)


def held_inspected():
    """Every tree the shared held-tree index records as inspected."""
    inspected = set()
    for value in obligations._HELD_RECORD_READS.values():
        inspected.update(tree.name for tree in value[2].inspected)
    return inspected


class Lookup:
    """Drive the store lookup, counting calls and failing a chosen tree."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.failing = set()
        real = open_run_index.rows_naming_trees

        def lookup(project, trees, root=None):
            names = [tree.name for tree in trees]
            self.calls.append(names)
            for tree in trees:
                if tree.name in self.failing:
                    raise RuntimeError("transient store failure")
            return real(project, trees, root)

        monkeypatch.setattr(open_run_index, "rows_naming_trees", lookup)

    def looked_up(self):
        return [name for names in self.calls for name in names]


def parked_tree(fleet):
    """A fourth tree whose record and registration exist while its dir does not.

    Its landed row is filed from the store's projection, never from a per-run
    document the incremental pass re-reads, so the row-lookup is its only route
    into the index.
    """
    tree = fleet.add(3)
    parked = tree.with_name("held-3-parked")
    tree.rename(parked)
    return tree, parked


def test_a_failed_lookup_leaves_the_tree_uninspected_and_retries(fleet, monkeypatch):
    lookup = Lookup(monkeypatch)
    tree, parked = parked_tree(fleet)
    assert fleet.refresh() == {"held-run-0", "held-run-1", "held-run-2"}
    assert tree.name not in held_inspected()

    parked.rename(tree)
    lookup.failing = {tree.name}
    assert fleet.refresh() == {"held-run-0", "held-run-1", "held-run-2"}
    assert tree.name in lookup.looked_up()
    # The failed lookup must not record the tree: otherwise the duty is lost
    # until an unrelated rewrite of the aggregate rebuilds the index.
    assert tree.name not in held_inspected()

    lookup.failing = set()
    assert fleet.refresh() == {
        "held-run-0",
        "held-run-1",
        "held-run-2",
        "held-run-3",
    }
    assert tree.name in held_inspected()


def test_a_successful_lookup_is_not_repeated_on_the_next_refresh(fleet, monkeypatch):
    lookup = Lookup(monkeypatch)
    tree, parked = parked_tree(fleet)
    assert fleet.refresh() == {"held-run-0", "held-run-1", "held-run-2"}

    parked.rename(tree)
    assert fleet.refresh() == {
        "held-run-0",
        "held-run-1",
        "held-run-2",
        "held-run-3",
    }
    assert lookup.looked_up().count(tree.name) == 1

    lookup.calls.clear()
    assert fleet.refresh() == {
        "held-run-0",
        "held-run-1",
        "held-run-2",
        "held-run-3",
    }
    assert lookup.looked_up() == []


def test_recovered_duty_view_equals_a_fresh_build(fleet, monkeypatch):
    lookup = Lookup(monkeypatch)
    tree, parked = parked_tree(fleet)
    fleet.refresh()
    parked.rename(tree)
    lookup.failing = {tree.name}
    fleet.refresh()
    lookup.failing = set()
    recovered = fleet.refresh()

    obligations._clear_cached_obligation_inputs()
    fresh = fleet.refresh()
    assert recovered == fresh
    assert "held-run-3" in recovered
