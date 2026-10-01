"""crew complete refuses while the checkout carries an open git operation.

Promotion commits the stores it writes into the checkout's index. When a peer
session has a merge, rebase or cherry-pick open in that checkout, a landing
commit moves the operation's first parent under it and a whole-index commit
could take the peer's staged work. Promotion must therefore refuse before
either store is written, and name the state it found.

Each open state is resolved with ``git rev-parse --git-path`` so that a linked
worktree, whose ``.git`` is a file pointing at its private directory, is read
where git actually keeps the marker rather than at a ``.git/`` that is absent.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

# marker name, the word the refusal must carry, and whether the marker is a
# directory (a rebase state) rather than a file.
OPEN_STATES = (
    ("MERGE_HEAD", "merge", "file"),
    ("rebase-merge", "rebase", "dir"),
    ("rebase-apply", "rebase", "dir"),
    ("CHERRY_PICK_HEAD", "cherry-pick", "file"),
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_pointer(repository: Path, run_id: str) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-26T12:00:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


def _marker_path(repository: Path, marker: str) -> Path:
    """Resolve a git state marker through the repository's own git directory."""
    resolved = _git(repository, "rev-parse", "--git-path", marker)
    path = Path(resolved)
    return path if path.is_absolute() else repository / path


def _open_state(repository: Path, marker: str, kind: str) -> None:
    path = _marker_path(repository, marker)
    if kind == "dir":
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("0" * 40 + "\n", encoding="utf-8")


@pytest.mark.parametrize(("marker", "state_word", "kind"), OPEN_STATES)
def test_complete_refuses_while_an_open_state_exists(
    repository: Path, marker: str, state_word: str, kind: str
) -> None:
    run_id = f"r-open-{marker}"
    _write_pointer(repository, run_id)
    _open_state(repository, marker, kind)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", root=repository)

    assert state_word in str(refusal.value)
    # Refused before either store was written: no ledger row, the live pointer
    # survives, and the plan carries no landing comment.
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()
    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert plan["comments"] == {}
    run_file = ledger.run_path(PROJECT, run_id, repository)
    assert not run_file.exists()


def test_a_clean_checkout_still_promotes(repository: Path) -> None:
    run_id = "r-open-clean"
    _write_pointer(repository, run_id)

    promoted = crew.complete(run_id, gate="passed", root=repository)

    assert promoted["record"]["run_id"] == run_id
    assert not pointer_path(run_id).exists()
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]


def test_a_linked_worktree_resolves_its_own_open_state(
    repository: Path, tmp_path: Path
) -> None:
    """A worktree's ``.git`` is a file; the marker resolves at its git dir."""
    worktree = tmp_path / "linked-worktree"
    _git(repository, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    # Promotion commits into the project's mount checkout, so point the mount
    # at the linked worktree: the marker must be read where that checkout keeps
    # it, not at the main repository's ``.git``.
    (tmp_path / "config" / "mounts.json").write_text(
        json.dumps({PROJECT: str(worktree / "docs")}), encoding="utf-8"
    )
    run_id = "r-open-linked-worktree"
    _write_pointer(repository, run_id)
    _open_state(worktree, "MERGE_HEAD", "file")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", root=worktree)

    assert "merge" in str(refusal.value)
    assert ledger.runs(PROJECT, root=worktree) == []
    assert pointer_path(run_id).is_file()
