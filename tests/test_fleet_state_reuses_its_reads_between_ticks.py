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
    assert counts["held_records"] > 0 and counts["ledger_load"] > 0, counts
    counts.clear()
    fleet.trees[1].rename(fleet.trees[1].with_name("displaced"))
    assert refresh() == expected - {"held-run-0", "held-run-1"} | {"new-holder"}
    assert counts["held_records"] > 0, counts
