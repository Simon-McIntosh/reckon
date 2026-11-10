"""A released worktree takes the ledger index it published with it.

A worktree resolves its own ledger path, so every worktree's reader publishes
a full-size index keyed to that checkout. The release path removed the tree and
left the index behind, where nothing resolved it again until someone ran a
sweep by hand, and none did: ``~/.cache/reckon`` held hundreds of gigabytes of
indexes recording checkouts that were gone. The release now unlinks the index
of the tree it releases, keeps one a live process holds open, and the run-less
sweep still reaps the backlog.

Every case works on a synthesised checkout and a temporary cache root under a
temporary configuration home; no real fleet directory is read or written.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from reckon import ledger
from tests.helpers.git import git as _git

gc_module = importlib.import_module("reckon.crew.gc")
promotion_release_module = importlib.import_module("reckon.crew.promotion_release")
routing_module = importlib.import_module("reckon.crew.routing")

PROJECT = "released-index-fixture"
SESSION = "released-index-session"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    # Project state is local to a checkout and never tracked, which is what
    # keeps a tree that has read its own ledger still clean for release.
    (repo / ".gitignore").write_text("docs/state/\n", encoding="utf-8")
    for index in range(4):
        (repo / f"tracked-{index}.txt").write_text(f"file {index}\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "."),
        ("commit", "-q", "-m", "test: seed the released-index fixture"),
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _worktree(repo: Path, name: str) -> Path:
    """A managed tree at the repository's head, clean and integrated."""
    root = routing_module._workspace_roots(repo)[0]
    tree = root / SESSION / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def _record(repo: Path, tree: Path, run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "worktree": str(tree),
        "repo": str(repo),
        "project": PROJECT,
        "pid": _dead_pid(),
    }


def _publish_index(checkout: Path) -> Path:
    """Write a ledger in ``checkout`` and read it, so its reader builds the index.

    Reading is what publishes an index, so the file here is produced by the
    same path a worker takes rather than by a fixture writing the cache naming
    scheme by hand.
    """
    ledger_file = ledger.ledger_path(PROJECT, checkout)
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    ledger_file.write_text(
        json.dumps({"data": {"members": [], "runs": [], "holds": [], "_version": 0}}),
        encoding="utf-8",
    )
    ledger.load(PROJECT, checkout)
    index = ledger._run_index_path(PROJECT, checkout)
    assert index.is_file(), f"the reader published no index at {index}"
    return index


@contextmanager
def _held_open(index: Path) -> Iterator[None]:
    """Hold SQLite's write lock on ``index``, as a reader mid-refresh does."""
    with closing(sqlite3.connect(index, isolation_level=None, timeout=0.0)) as held:
        held.execute("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            held.execute("ROLLBACK")


def test_releasing_a_worktree_removes_only_its_own_index(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    released_tree = _worktree(repository, "released")
    kept_tree = _worktree(repository, "kept")
    released_index = _publish_index(released_tree)
    kept_index = _publish_index(kept_tree)

    result = promotion_release_module._release_run_workspace(
        _record(repository, released_tree, "r-20261110T000000000000-released")
    )

    assert result["worktree_released"] is True, result
    assert not released_tree.exists()
    assert result["ledger_index_removed"] == str(released_index), result
    assert not released_index.exists(), "the released tree's index outlived it"
    assert kept_index.is_file(), "a peer worktree lost its index"
    assert f"removed ledger index {released_index}" in capsys.readouterr().out


def test_an_index_a_live_process_holds_open_is_kept(repository: Path) -> None:
    tree = _worktree(repository, "held")
    index = _publish_index(tree)

    with _held_open(index):
        result = promotion_release_module._release_run_workspace(
            _record(repository, tree, "r-20261110T000000000001-held")
        )
        assert index.is_file(), "a held index was unlinked under its reader"

    assert result["worktree_released"] is True, result
    assert "ledger_index_removed" not in result
    assert result["ledger_index_withheld"] == "a live process holds the index open"


def test_the_runless_sweep_removes_an_index_whose_checkout_is_gone(
    repository: Path,
) -> None:
    gone_checkout = repository.parent / "gone-checkout"
    (gone_checkout / "docs" / "state" / PROJECT).mkdir(parents=True)
    index = _publish_index(gone_checkout)
    assert ledger.recorded_ledger_path(index) is not None

    shutil.rmtree(gone_checkout)
    assert not gone_checkout.exists()

    report = gc_module.garbage_collect(repo=repository, project=PROJECT, apply=True)

    rows = [row for row in report["ledger_indexes"] if row["path"] == str(index)]
    assert len(rows) == 1, report["ledger_indexes"]
    assert rows[0]["reason"] == "recorded-checkout-gone"
    assert rows[0]["removed"] is True
    assert not index.exists()


def test_the_sweep_leaves_an_index_whose_checkout_exists(repository: Path) -> None:
    live_index = _publish_index(repository)
    assert live_index.is_file()

    report = gc_module.garbage_collect(repo=repository, project=PROJECT, apply=True)

    assert live_index.is_file()
    assert all(row["path"] != str(live_index) for row in report["ledger_indexes"])
