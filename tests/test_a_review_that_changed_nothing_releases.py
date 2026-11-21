"""A review that changed nothing releases its workspace and reads recorded.

A read-only review has no repository work to commit, so its manifest states the
absence in the ``commits`` field instead of a revision — bare ``none``, ``none
(…)`` or ``none — …``. The list reader leaves that sentence where a citation
list would be, and the release step resolved the prose as a revision, so a run
whose worktree held no change and whose record was stored was refused with
``manifest commit '…' does not resolve`` and classified blocked or unreadable.

The declaration is not a citation. The release step judges the field through
the same reader the promotion uses, which recognises the three observed shapes,
so a commitless review releases and reads ``recorded`` while a review whose
worktree holds a change is still refused naming it and an implement run citing
a commit that does not resolve is still refused.

Every case synthesises its own repository, worktree, pointer and ledger under a
temporary configuration home, and the last test asserts the workstation's real
pointer directory is untouched, because an isolated read does not prove an
isolated write.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import pending_landing, recovery
from tests.helpers.git import git as _git

PROJECT = "release-a-review-fixture"
PROSE_SPELLINGS = (
    "none (read-only review run; nothing committed)",
    "none — a read-only run has no commit to cite",
    "none",
)


@pytest.fixture(autouse=True)
def real_pointer_directory_is_not_a_fixture_target() -> None:
    """No case may reach the workstation's real crew pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    before = set(real_live.glob("*.json")) if real_live.exists() else set()
    yield
    after = set(real_live.glob("*.json")) if real_live.exists() else set()
    assert after == before


@pytest.fixture()
def repository(home: Path) -> Path:
    """A real repository with one seed commit and its mount registered."""
    root = home.parent / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "test: seed the repository")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _worktree(repository: Path, home: Path, base: str) -> Path:
    tree = home.parent / "worktrees" / "review-run"
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", "--quiet", str(tree), base)
    return tree


def _pointer(
    repository: Path,
    home: Path,
    tree: Path,
    *,
    run_id: str,
    role: str,
    commits: str,
) -> dict:
    """Write the run's manifest and live pointer, and return the pointer."""
    manifest = home.parent / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}-node\nstatus: complete\ncommits: {commits}\n"
        "changed_paths: none\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(tree),
        "base_sha": _git(repository, "rev-parse", "HEAD"),
        "manifest_path": str(manifest),
        "role": role,
        "phase": "working",
        "created_at": datetime.now(UTC).isoformat(),
        "node": {"id": f"{run_id}-node", "role": role},
    }
    crew._write_json(crew.pointer_path(run_id), pointer)
    return pointer


def _recorded_row(repository: Path, run_id: str) -> None:
    """The ledger row a commitless promotion writes: the run is settled."""
    row = ledger.run_path(PROJECT, run_id, str(repository))
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "commits": [],
                "no_primary_change": "the report is the deliverable",
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "spelling", PROSE_SPELLINGS, ids=["parenthesised", "dashed", "bare"]
)
def test_a_commitless_review_releases_and_reads_recorded(
    repository: Path, home: Path, spelling: str
) -> None:
    """Each observed spelling releases the workspace and settles as recorded."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _worktree(repository, home, base)
    run_id = "r-review-changed-nothing"
    pointer = _pointer(
        repository, home, tree, run_id=run_id, role="review", commits=spelling
    )
    _recorded_row(repository, run_id)

    # The release step's own reader must permit the release. At the base
    # revision the prose spellings are resolved as a revision and refused.
    assert pending_landing.landed_workspace_refusal(pointer, repository, "main") == ""

    verdict = recovery.classify_pointer(
        crew.read_pointer(run_id), now_seconds=time.time()
    )
    assert verdict["classification"] == "recorded"
    assert verdict["classification"] not in {"blocked", "unreadable"}


def test_a_review_with_an_uncommitted_change_is_refused_naming_it(
    repository: Path, home: Path
) -> None:
    """A worktree holding a change is still kept, and the change is named."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _worktree(repository, home, base)
    (tree / "left-behind.txt").write_text("uncommitted work\n", encoding="utf-8")
    pointer = _pointer(
        repository,
        home,
        tree,
        run_id="r-review-dirty",
        role="review",
        commits="none — read-only review",
    )

    refusal = pending_landing.landed_workspace_refusal(pointer, repository, "main")

    assert "uncommitted" in refusal
    assert "left-behind.txt" in refusal


def test_an_implement_run_citing_an_unresolvable_commit_is_refused(
    repository: Path, home: Path
) -> None:
    """A committing role's prose is not read as a declaration of absence."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _worktree(repository, home, base)
    pointer = _pointer(
        repository,
        home,
        tree,
        run_id="r-implement-unresolved",
        role="implement",
        commits="none (read-only review run; nothing committed)",
    )

    refusal = pending_landing.landed_workspace_refusal(pointer, repository, "main")

    assert "does not resolve" in refusal
