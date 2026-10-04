"""A blank peer worktree field grants nothing to the tree the promotion starts in.

The boundary scan resolved each live peer's tree as
``Path(str(pointer.get("worktree") or ""))``, and ``Path("")`` is ``Path(".")``:
a live pointer whose worktree field was blank matched whichever directory the
promotion happened to start in. Where another live pointer legitimately held
that tree, the blank pointer's declared write paths were granted to it, and an
uncommitted edit at one of those paths was exempted from the boundary walk
instead of reported.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import promotion
from reckon.crew.runs import pointer_path


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _seed_repository(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    (root / "file.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "-m", "seed")


def _write_pointer(run_id: str, **fields: object) -> None:
    path = pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, **fields}), encoding="utf-8")


@pytest.fixture()
def unrelated_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository the run was never dispatched for, as the cwd.

    The crew home is a temporary directory too, so a reader that reaches for
    the live fleet reads an empty store rather than the workstation's own.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "unrelated"
    _seed_repository(root)
    monkeypatch.chdir(root)
    return Path.cwd()


def test_blank_peer_worktree_grants_no_path_matches_the_ambient_tree(
    unrelated_repository: Path,
) -> None:
    """The blank pointer donates nothing, so the stray edit stays charged.

    Two live pointers are present: one legitimately holds the ambient tree, and
    one names no worktree at all while declaring the edited path. The blank
    field must not match the ambient tree, so the edit at the promoting run's
    declared path is reported as a main-checkout violation.
    """
    root = unrelated_repository
    (root / "file.txt").write_text("an uncommitted edit\n", encoding="utf-8")
    _write_pointer("r-blank-peer-holder", phase="working", worktree=str(root))
    _write_pointer(
        "r-blank-peer",
        worktree="",
        repo=str(root),
        node={"write_paths": ["file.txt"]},
    )
    record = {
        "project": "blank-worktree-fixture",
        "repo": str(root),
        "worktree": "",
        "node": {"write_paths": ["file.txt"]},
        "repository_tree_snapshot": {
            "trees": [
                {
                    "path": str(root),
                    "available": True,
                    "status_digest": "the digest before the edit",
                    "status_entries": [],
                }
            ]
        },
    }

    violations = promotion._repository_tree_boundary_violations(
        "r-blank-boundary-peer",
        record,
    )

    assert len(violations) == 1
    assert "file.txt" in violations[0]
    assert "main checkout" in violations[0]
