"""``worktree-held`` names only a tree that is still on disk.

Git keeps a path in its worktree registry until the registry is pruned, so a
tree whose directory was deleted out from under the registration is still
listed by ``git worktree list``. The held-tree reader reads that listing, so
without an existence check it raises ``worktree-held`` for a registration with
nothing behind it — and the duty's remedy is a project-wide ``crew gc --apply``
that would sweep peers' trees to clear the tree entry alone.

Every record here is built by the ledger's own constructor and published
through the ledger's own writer, so no case asserts against keys this test
invented.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "held-tree-exists-fixture"
SESSION = "s22-fixture"
OBSERVED_AT = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
COMPLETED_AT = OBSERVED_AT - timedelta(minutes=5)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the held-tree fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _worktree(root: Path, node: str) -> Path:
    """Register a tree the way a dispatched run leaves one behind."""
    path = root.parent / "managed-worktrees" / SESSION / node
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _registered(root: Path) -> list[str]:
    """Every path Git's registry lists, whether or not it exists on disk."""
    return [
        line.removeprefix("worktree ")
        for line in _git(root, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def _produced(run_id: str, node: str, *, worktree: Path) -> dict[str, Any]:
    """One record as the producer builds it, with its retention attached."""
    record = ledger.build_record(
        run_id=run_id,
        plan="fixture-plan",
        gate="failed",
        node=node,
        completed_at=COMPLETED_AT.isoformat(),
    )
    record["worktree_retention"] = {
        "classification": "retained-for-resume",
        "worktree": str(worktree.resolve()),
        "session_id": "fixture-session",
        "session_source": "pointer",
        "retained_at": COMPLETED_AT.isoformat(),
    }
    return record


def _held(root: Path) -> list[dict[str, Any]]:
    return obligations_module._held_worktrees(PROJECT, SESSION, now=OBSERVED_AT)


def test_a_promoted_run_whose_tree_was_deleted_is_not_held(fleet: Path) -> None:
    """A registration with no directory behind it holds nothing."""
    tree = _worktree(fleet, "alpha-deleted")
    ledger.append_run(
        PROJECT,
        _produced(
            "r-20261002T080000000000-alpha-deleted", "alpha-deleted", worktree=tree
        ),
        root=fleet,
        allow_create=True,
    )
    shutil.rmtree(tree)

    # The absence of a duty is a claim about a reader, so the registration the
    # reader inspects is shown to be really there: Git still lists the path.
    assert str(tree.resolve()) in _registered(fleet)
    assert not tree.is_dir()

    assert _held(fleet) == []


def test_a_promoted_run_whose_tree_remains_is_held(fleet: Path) -> None:
    """A tree still on disk is a held tree, and the duty names its run."""
    tree = _worktree(fleet, "beta-kept")
    ledger.append_run(
        PROJECT,
        _produced("r-20261002T080100000000-beta-kept", "beta-kept", worktree=tree),
        root=fleet,
        allow_create=True,
    )

    held = _held(fleet)

    assert [item["run_id"] for item in held] == ["r-20261002T080100000000-beta-kept"]
    assert held[0]["kind"] == "worktree-held"


def test_a_record_showing_the_tree_released_is_not_held(fleet: Path) -> None:
    """A promotion record that shows the tree released names no held tree."""
    tree = _worktree(fleet, "gamma-released")
    record = _produced(
        "r-20261002T080200000000-gamma-released", "gamma-released", worktree=tree
    )
    record["release"] = {"worktree_released": True}
    ledger.append_run(PROJECT, record, root=fleet, allow_create=True)

    assert tree.is_dir()
    assert _held(fleet) == []
