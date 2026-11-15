"""``crew gc --run X`` removes the trash X left and nothing else.

A release vacates a worktree by renaming it into the trash root before the copy
is deleted, so the trash root is shared by every run releasing a tree under the
same workspace. A gc scoped to one run therefore has to decide which entries are
that run's, or a destructive command named for one run reaches another's
leftovers. The label rule is the release's own: a copy is written as
``<label>-<stamp>`` with the releasing run's id when it had one, and the tree's
own directory name otherwise.

Every case works on a synthesised repository and a temporary config home; no
real fleet directory is read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import routing
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git

PROJECT = "run-scoped-gc-fixture"
SESSION = "run-scoped-gc-session"
NODE = "run-scoped-gc-node"

RUN_ID = "r-20261115T010000000000-run-scoped-gc-node"
OTHER_RUN_ID = "r-20261115T010000000001-other-node"
OTHER_NODE = "other-node"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout with a mounted project and a managed tree root."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKER_ADD_ROOT", str(tmp_path / "plain-roots"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "."),
        ("commit", "-q", "-m", "test: seed the run-scoped-gc fixture"),
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _node_tree(repo: Path, node: str) -> Path:
    """A registered tree named for a node, as a dispatch creates one."""
    root = routing._workspace_roots(repo)[0]
    tree = root / SESSION / node
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def _park(repo: Path, run_id: str, tree: Path) -> None:
    """Record a live pointer naming the run's own tree, so gc can resolve it."""
    _write_json(
        Path(pointer_path(run_id)),
        {
            "run_id": run_id,
            "pid": 0,
            "worktree": str(tree),
            "repo": str(repo),
            "phase": "blocked",
        },
    )


def _trash_root(repo: Path) -> Path:
    return routing._workspace_roots(repo)[0] / routing.WORKTREE_TRASH_DIRNAME


def _plant_trash(repo: Path, name: str) -> Path:
    """Leave one released tree's copy in the workspace trash root."""
    root = _trash_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    entry = root / f"{name}-20261115T020000000000Z"
    entry.mkdir()
    (entry / "leftover.txt").write_text("leftover\n", encoding="utf-8")
    return entry


def _removed_paths(report: dict) -> list[str]:
    return [row["path"] for row in report["worktree_trash"] if row["removed"]]


def test_a_run_scoped_gc_removes_only_its_own_trash_and_prints_each_removal(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _node_tree(repository, NODE)
    _park(repository, RUN_ID, tree)

    # Two labels belong to the named run: its id, written by a promotion, and
    # its own tree's directory name, written by a gc that released the tree.
    mine_by_run = _plant_trash(repository, RUN_ID)
    mine_by_tree = _plant_trash(repository, NODE)
    # Neither belongs to it: another run's copy and another node's copy sit in
    # the same root because the root is shared by every release beneath it.
    other_by_run = _plant_trash(repository, OTHER_RUN_ID)
    other_by_tree = _plant_trash(repository, OTHER_NODE)

    report = routing.garbage_collect(repo=repository, run_id=RUN_ID, apply=True)

    assert _removed_paths(report) == [str(mine_by_run), str(mine_by_tree)], report[
        "worktree_trash"
    ]
    assert not mine_by_run.exists()
    assert not mine_by_tree.exists()
    assert other_by_run.is_dir(), "a run-scoped gc removed another run's trash"
    assert other_by_tree.is_dir(), "a run-scoped gc removed another node's trash"

    printed = capsys.readouterr().out
    assert str(mine_by_run) in printed, printed
    assert str(mine_by_tree) in printed, printed
    assert str(other_by_run) not in printed, printed
    assert str(other_by_tree) not in printed, printed


def test_an_unscoped_gc_still_sweeps_every_trash_entry(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = [
        _plant_trash(repository, RUN_ID),
        _plant_trash(repository, NODE),
        _plant_trash(repository, OTHER_RUN_ID),
        _plant_trash(repository, OTHER_NODE),
    ]

    report = routing.garbage_collect(repo=repository, project=PROJECT, apply=True)

    assert sorted(_removed_paths(report)) == sorted(str(entry) for entry in entries)
    for entry in entries:
        assert not entry.exists(), entry
    printed = capsys.readouterr().out
    for entry in entries:
        assert str(entry) in printed, printed
    assert list(_trash_root(repository).iterdir()) == []
