"""Worktrees share a cache; retention removes only disposable, unheld files."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from reckon import ledger
from reckon.crew import gc

PROJECT = "cache-test"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    source = ledger.ledger_path(PROJECT, repo)
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps({"data": {"members": [], "holds": [], "runs": [], "_version": 1}})
    )
    runs = source.parent / "runs"
    runs.mkdir()
    (runs / "sample.json").write_text(
        json.dumps({"run_id": "sample", "node": "shared"})
    )
    _git(
        repo,
        "add",
        "docs/state/cache-test/crew.json",
        "docs/state/cache-test/runs/sample.json",
    )
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "test: seed ledger",
        "-m",
        "Provide ledger content for cache readers.",
    )
    trees = [tmp_path / "reader-a", tmp_path / "reader-b"]
    for tree in trees:
        _git(repo, "worktree", "add", "--detach", str(tree), "HEAD")
    return repo, trees


def test_two_worktrees_share_one_cache_and_second_reader_builds_nothing(repository):
    repo, (first, second) = repository
    paths = [ledger._run_index_path(PROJECT, root) for root in (repo, first, second)]
    assert len(set(paths)) == 1, paths
    with (
        patch.object(
            ledger, "_load_aggregate", wraps=ledger._load_aggregate
        ) as aggregate,
        patch.object(ledger, "_read_run", wraps=ledger._read_run) as document,
    ):
        expected = ledger.load(PROJECT, first)
        assert aggregate.call_count == 1
        assert document.call_count == 1
        aggregate.reset_mock()
        document.reset_mock()
        assert ledger.load(PROJECT, second) == expected
        assert aggregate.call_count == document.call_count == 0
    assert list(paths[0].parent.glob("ledger-*.sqlite")) == [paths[0]]


@pytest.mark.parametrize(
    "source_kind", ["aggregate", "document", "added-document", "removed-document"]
)
def test_differing_worktree_reads_through_without_changing_shared_cache(
    repository, source_kind
):
    repo, (first, second) = repository
    expected = ledger.load(PROJECT, repo)
    index = ledger._run_index_path(PROJECT, first)
    before = index.read_bytes()
    source = ledger.ledger_path(PROJECT, first)
    document = source.parent / "runs" / "sample.json"
    if source_kind == "aggregate":
        payload = json.loads(source.read_text())
        payload["data"]["members"] = [{"id": "local"}]
        source.write_text(json.dumps(payload))
    elif source_kind == "document":
        document.write_text(json.dumps({"run_id": "sample", "node": "local"}))
    elif source_kind == "added-document":
        (document.parent / "extra.json").write_text(json.dumps({"run_id": "extra"}))
    else:
        document.unlink()
    assert ledger.load(PROJECT, first) == ledger.load(PROJECT, first, use_index=False)
    assert ledger.load(PROJECT, first) != expected
    assert ledger.run_ids(PROJECT, first) == {
        row["run_id"] for row in ledger.load(PROJECT, first)[0]["runs"]
    }
    assert ledger.load(PROJECT, second) == expected
    assert index.read_bytes() == before


def test_cache_identity_includes_repository_and_projection(
    repository, tmp_path, monkeypatch
):
    repo, _trees = repository
    first = ledger._run_index_path(PROJECT, repo)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    assert ledger._run_index_path(PROJECT, other) != first
    monkeypatch.setattr(ledger, "_RUN_INDEX_VERSION", ledger._RUN_INDEX_VERSION + 1)
    assert ledger._run_index_path(PROJECT, repo) != first


def _seed_index(path, recorded):
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE metadata (name TEXT PRIMARY KEY, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO metadata VALUES (?, ?)",
            (ledger.LEDGER_INDEX_PATH_KEY, str(recorded)),
        )
        connection.commit()


def test_retention_prunes_orphans_and_old_picker_views_but_keeps_fresh_and_held(
    repository, tmp_path, monkeypatch, capsys
):
    repo, _trees = repository
    cache = ledger._run_index_path(PROJECT, repo).parent
    cache.mkdir(parents=True, exist_ok=True)
    orphan = cache / "ledger-gone.sqlite"
    held_index = cache / "ledger-held.sqlite"
    live_index = cache / "ledger-live.sqlite"
    for path in (orphan, held_index):
        _seed_index(path, tmp_path / "gone" / "crew.json")
    _seed_index(live_index, ledger.ledger_path(PROJECT, repo))
    old = cache / "picker-budget-view-old.json"
    held = cache / "picker-budget-view-held.json"
    fresh = cache / "picker-budget-view-fresh.json"
    unrelated = cache / "picker-other.json"
    for path in (old, held, fresh, unrelated):
        path.write_text("{}")
    now = datetime.now(tz=UTC)
    stamp = (now - timedelta(days=8)).timestamp()
    for path in (old, held, unrelated):
        os.utime(path, (stamp, stamp))
    monkeypatch.setattr(
        gc,
        "_retention_ledger_records",
        lambda *args: (tmp_path / "absent-runs", {}, {}, {}),
    )
    monkeypatch.setattr(gc, "list_live", list)
    monkeypatch.setattr(gc, "_live_review_targets", set)
    monkeypatch.setattr(gc, "settle_run_scratch", lambda **kwargs: {"removed": []})
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; handles=[open(p, 'rb') for p in sys.argv[1:]]; print('holding', flush=True); sys.stdin.read()",
            str(held),
            str(held_index),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "holding"
        # Positive control: prove the holder instrument sees the child's open file.
        assert any(row["pid"] == holder.pid for row in gc._processes_holding(held))
        gc.retention_sweep(apply=False, now=now)
        assert all(path.exists() for path in (orphan, old, held, held_index, fresh))
        gc.retention_sweep(apply=True, now=now, log=tmp_path / "retention.log")
        assert not orphan.exists()
        assert not old.exists()
        assert all(
            path.is_file() for path in (held, held_index, fresh, live_index, unrelated)
        )
        printed = capsys.readouterr().err
        assert str(orphan) in printed and str(old) in printed
        assert str(orphan) in (tmp_path / "retention.log").read_text()
        assert str(old) in (tmp_path / "retention.log").read_text()
    finally:
        holder.communicate(timeout=10)
    gc.retention_sweep(apply=True, now=now, log=tmp_path / "retention.log")
    assert not held.exists()
    assert not held_index.exists()
