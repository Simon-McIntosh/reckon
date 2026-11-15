"""A fleet read shares indexed stat views and notices the next indexed write."""

from __future__ import annotations

import json
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from reckon import file_memo
from reckon.crew import obligation_snapshot, review, review_records, runs
from tests.helpers.git import git

PROJECT = "fixture"
POINTERS = 30
ENTRIES = 2000
NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def indexed_fleet(home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    seed = repo / "seed.txt"
    seed.write_text("fixture\n")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed fleet", "-m", "Supply a stable head."),
    ):
        git(repo, *args)
    head = git(repo, "rev-parse", "HEAD").strip()
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(docs)}))
    directory = review.review_store_root() / PROJECT
    directory.mkdir(parents=True)
    for index in range(ENTRIES):
        run_id = f"run-{index:04d}"
        record = {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_head_sha": head,
            "status": "parsed",
            "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
            "absent": [],
            "total": 18 * len(review.REVIEW_DIMENSIONS),
        }
        (directory / f"{run_id}.json").write_text(json.dumps(record))
        if index < POINTERS:
            manifest = runs.run_dir(run_id) / "manifest.md"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("status: complete\ncommits: []\noutside_scope: none\n")
            runs._write_json(
                runs.pointer_path(run_id),
                {
                    "run_id": run_id,
                    "project": PROJECT,
                    "session": "coordinator",
                    "repo": str(repo),
                    "worktree": str(repo),
                    "base_sha": head,
                    "process_alive": False,
                    "launch": "in-harness",
                    "role": "implement",
                    "manifest_path": str(manifest),
                    "node": {"id": run_id, "write_paths": ["seed.txt"]},
                },
            )
    document = review_records.store_index_document(directory)
    assert document is not None and len(document["entries"]) == ENTRIES
    file_memo.clear()
    return directory


@pytest.mark.parametrize("inside_tick", [False, True], ids=["outside", "inside"])
def test_each_store_view_is_built_once(indexed_fleet, inside_tick, record_property):
    assert review_records._TICK_REVIEW_STORES.get() is None
    counts = Counter()
    real = review_records._document_stat_view

    def counted(directory, *args):
        counts[str(directory)] += 1
        return real(directory, *args)

    with (
        patch.object(review_records, "_document_stat_view", counted),
        review_records.review_store_tick() if inside_tick else nullcontext(),
    ):
        state = obligation_snapshot.fleet_state(PROJECT, now=NOW)

    classifications = {
        row["run_id"]: [row["classification"], row.get("recovery_classification")]
        for row in state.rows
    }
    record_property("classifications", json.dumps(classifications, sort_keys=True))
    record_property("builds", json.dumps(dict(counts), sort_keys=True))
    assert len(state.pointers) == POINTERS
    assert len(classifications) == POINTERS
    assert all(pair[1] != "unreadable" for pair in classifications.values())
    assert counts[str(indexed_fleet)] >= 1, "the measured path was never reached"
    assert max(counts.values()) <= 1, dict(counts)


@pytest.mark.parametrize("inside_tick", [False, True], ids=["outside", "inside"])
def test_an_index_write_rebuilds_the_next_read(indexed_fleet, inside_tick):
    directory = indexed_fleet
    real = review_records._document_stat_view
    builds = []

    def counted(*args):
        builds.append(args[0])
        return real(*args)

    def read():
        with review_records.review_store_tick() if inside_tick else nullcontext():
            obligation_snapshot.fleet_state(PROJECT, now=NOW)
            return review_records._tick_store(directory)

    with patch.object(review_records, "_document_stat_view", counted):
        first = read()
        builds.clear()
        new_path = directory / "added.json"
        review_records.record_written(
            new_path,
            {
                "project": PROJECT,
                "reviewed_run_id": "added",
                "review_run_id": "added-review",
            },
        )
        second = read()
    assert new_path not in first
    assert new_path in second
    assert builds.count(directory) == 1, Counter(builds)
    assert len(second) == ENTRIES + 1


def test_rebuilt_generations_and_distinct_stores_have_distinct_views(
    tmp_path, monkeypatch
):
    directories = [tmp_path / name for name in ("first", "second")]
    documents = {
        directory: {
            "generation": "initial",
            "version": 1,
            "entries": {f"{directory.name}.json": {"entry_version": 1}},
        }
        for directory in directories
    }
    monkeypatch.setattr(review_records, "store_index_document", documents.get)
    real = review_records._document_stat_view
    with patch.object(review_records, "_document_stat_view", wraps=real) as build:
        first = review_records._tick_store(directories[0])
        second = review_records._tick_store(directories[1])
        assert set(first) == {directories[0] / "first.json"}
        assert set(second) == {directories[1] / "second.json"}
        assert review_records._tick_store(directories[0]) is first
        assert build.call_count == 2
        documents[directories[0]] = {
            "generation": "rebuilt",
            "version": 1,
            "entries": {"replacement.json": {"entry_version": 1}},
        }
        replacement = review_records._tick_store(directories[0])
        assert set(replacement) == {directories[0] / "replacement.json"}
        assert build.call_count == 3
