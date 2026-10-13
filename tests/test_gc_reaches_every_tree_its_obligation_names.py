"""gc reaches every tree its obligation names.

A run promoted before its commits reach main has its tree withheld, which is
correct: the promotion record names no retained path, because a withheld tree
is not a *retained* one. Once the coordinator merges, the obligations view
reports the tree as ``worktree-held`` and names ``crew gc --run <id> --apply``
as the remedy — but the two disagreed about which tree belongs to the run. The
obligations view matched a tree by the promoted path or, failing that, by the
node id the tree's directory is named for; ``_run_worktree_path`` read only the
promoted path fields, so a withheld row's ``worktree_retention`` was null and no
``worktree`` field was present, and the remedy refused with "no record names a
worktree".

``crew gc --run`` now resolves a run's tree from the same rule the obligations
view uses, shared from one predicate, so every tree an obligation names is one
its remedy can reach. The release rules are unchanged: an unintegrated or dirty
tree is still withheld.

Every case works on a synthesised repository, ledger and workspace under a
temporary config home; no real fleet directory is read or written.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, ledger
from tests.helpers.git import git as _git

routing_module = importlib.import_module("reckon.crew.routing")
obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "gc-reaches-fixture"
SESSION = "gc-reaches-session"
RUN_WITHHELD = "r-20261013T130000000000-withheld-node"
NODE = "withheld-node"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the gc fixture"),
    ):
        _git(repository, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _withheld(repository: Path, name: str) -> tuple[Path, str]:
    """A clean worktree carrying one commit unreachable from the integration head."""
    root = routing_module._workspace_roots(repository)[0]
    tree = root / SESSION / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    (tree / "unique.py").write_text("commits nowhere else\n", encoding="utf-8")
    _git(tree, "add", "unique.py")
    _git(tree, "commit", "-q", "-m", "test: work reachable nowhere else")
    assert _git(tree, "status", "--porcelain").strip() == ""
    return tree, _git(tree, "rev-parse", "HEAD")


def _promoted_withheld(repository: Path, run_id: str, tree: Path) -> None:
    """A promoted run whose row names its withheld tree by node id alone.

    A withheld tree is not a retained one, so the row carries no
    ``worktree_retention`` and no ``worktree`` field: the node id is the only
    evidence that names the tree.
    """
    record = ledger.build_record(
        run_id=run_id,
        plan="fixture-plan",
        gate="passed",
        project=PROJECT,
        node=tree.name,
    )
    assert record.get("worktree_retention") is None
    assert not record.get("worktree")
    ledger.append_run(PROJECT, record, root=repository, allow_create=True)


def _gc_run(repository: Path, run_id: str) -> tuple[int, dict]:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(repository),
            "--project",
            PROJECT,
            "--run",
            run_id,
            "--apply",
        ],
    )
    return result.exit_code, json.loads(result.output)


def _removed(payload: dict) -> set[str]:
    return {str(Path(item).resolve()) for item in payload["removed_worktrees"]}


def test_a_merged_withheld_tree_is_released_by_gc_run(repo: Path) -> None:
    """The merged run's withheld tree is now reachable, so gc --run releases it."""
    tree, head = _withheld(repo, NODE)
    _promoted_withheld(repo, RUN_WITHHELD, tree)
    _git(repo, "merge", "--no-ff", "-q", head, "-m", "test: merge the withheld tree")

    exit_code, payload = _gc_run(repo, RUN_WITHHELD)

    assert exit_code == 0, payload
    assert str(tree.resolve()) in _removed(payload), payload
    assert not tree.is_dir()


def test_an_unmerged_withheld_tree_is_withheld_by_gc_run(repo: Path) -> None:
    """The run's tree is still unreachable, so gc --run keeps it and moves nothing."""
    tree, _head = _withheld(repo, NODE)
    _promoted_withheld(repo, RUN_WITHHELD, tree)

    exit_code, payload = _gc_run(repo, RUN_WITHHELD)

    assert exit_code == 0, payload
    assert str(tree.resolve()) not in _removed(payload), payload
    assert tree.is_dir()
    row = next(
        item
        for item in payload["worktrees"]
        if str(Path(item["path"]).resolve()) == str(tree.resolve())
    )
    assert row["classification"] == "unintegrated"


def test_the_obligations_view_and_gc_name_the_same_tree(repo: Path) -> None:
    """The held tree the obligations view names is the one gc --run resolves."""
    tree, _head = _withheld(repo, NODE)
    _promoted_withheld(repo, RUN_WITHHELD, tree)

    held = obligations_module._held_worktree_records(PROJECT)
    assert tree.resolve() in held, held
    assert str(held[tree.resolve()].get("run_id") or "") == RUN_WITHHELD

    view = obligations_module.obligations(PROJECT, SESSION)
    item = next(
        duty
        for duty in view["obligations"]
        if duty["run_id"] == RUN_WITHHELD and duty["kind"] == "worktree-held"
    )
    assert f"--run {RUN_WITHHELD}" in item["next_command"]

    assert (
        routing_module._run_worktree_path(repo, PROJECT, RUN_WITHHELD) == tree.resolve()
    )
