"""A run whose worktree directory is gone reconciles, it does not crash.

``crew complete`` reaches a run's repository state through
``_worktree_repository_changes``, whose untracked measurement iterates the
output of ``_worktree_git_paths``. That helper answers ``None`` when the tree
cannot be read — the honest "nothing measurable here" rather than "no change" —
and a worktree directory removed while git still lists it is exactly that case.
Iterating ``None`` raises ``TypeError``, so the whole promotion printed no JSON
payload and landed nothing, which is the one outcome this plan's §10 rules out:
a run is always reconcilable from what survives it. Two cases are shown here,
both over a worktree that is removed while its registration survives. A
commitless promotion that records no commit reads as no repository change and
promotes, the absence named in the released worktree payload. A promotion whose
manifest cites a commit is refused by name — the existing no-referent
acknowledgement — and never with an exception.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "sample"
PLAN = "plan-a"


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
    """A synthetic config home and committed repository, isolated from the fleet."""
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
            "impl": 0.0,
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


def _removed_worktree(repository: Path, tmp_path: Path, name: str) -> Path:
    """A registered worktree whose directory has been removed, registration intact."""
    worktree = tmp_path / "worktree-pool" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    shutil.rmtree(worktree)
    # The registration outlives the directory: git still lists the tree, which
    # is the state a run whose worktree was reclaimed underneath it is in.
    listed = _git(repository, "worktree", "list", "--porcelain")
    assert str(worktree) in listed
    assert not worktree.is_dir()
    return worktree


def _pointer(
    repository: Path,
    run_id: str,
    *,
    worktree: Path,
    base: str,
    role: str,
    manifest: Path,
    write_paths: list[str] | None = None,
    extra: dict | None = None,
) -> None:
    pointer: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(worktree),
        "base_sha": base,
        "launch": "in-harness",
        "role": role,
        "backend": "native",
        "created_at": "2026-10-01T06:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": "node-vanished",
            "plan": PLAN,
            "section": "s1",
            "write_paths": list(write_paths or []),
        },
    }
    if extra:
        pointer.update(extra)
    _write_json(pointer_path(run_id), pointer)


# ── Case A: a commitless promotion with no commit cites nothing ─────────────


def test_a_commitless_promotion_reads_a_vanished_worktree_as_no_change(
    repository: Path, tmp_path: Path
) -> None:
    """No worktree to measure is no repository change, and the absence is named."""
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-vanished-no-commit"
    worktree = _removed_worktree(repository, tmp_path, run_id)
    manifest = run_dir(run_id) / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-vanished\n"
        "status: complete\n"
        "changed_paths: none\n"
        "commits: none\n"
        "tests: not applicable\n",
        encoding="utf-8",
    )
    _pointer(
        repository,
        run_id,
        worktree=worktree,
        base=base,
        role="review",
        manifest=manifest,
    )

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="landed from a run whose worktree was already released",
        root=repository,
    )

    # A parsed JSON payload: the bug printed none and raised instead.
    assert isinstance(json.loads(json.dumps(promoted)), dict)
    assert promoted["pointer_removed"] is True
    assert promoted["release"]["worktree_withheld"] == "tree is no longer available"
    assert not pointer_path(run_id).exists()
    assert [row["run_id"] for row in ledger.runs(PROJECT, root=repository)] == [run_id]


# ── Case B: a promotion that cites a commit gets a named refusal ─────────────


def test_a_promotion_citing_a_commit_refuses_a_vanished_worktree_by_name(
    repository: Path, tmp_path: Path
) -> None:
    """No worktree to attribute boundary changes to is a named refusal, not a crash."""
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-vanished-cited-commit"
    worktree = _removed_worktree(repository, tmp_path, run_id)
    # A declared path made dirty in the main checkout after the baseline snapshot:
    # with the run's own worktree gone those edits cannot be attributed to this
    # run, which is what the no-referent acknowledgement exists to report.
    declared = repository / "declared"
    declared.mkdir()
    (declared / "extra.txt").write_text("peer work\n", encoding="utf-8")
    snapshot = {
        "trees": [
            {"path": str(repository), "status_digest": "before", "status_entries": []}
        ]
    }
    manifest = run_dir(run_id) / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-vanished\n"
        "status: complete\n"
        f"commits: {base}\n"
        "changed_paths: none\n"
        "tests: not applicable\n",
        encoding="utf-8",
    )
    _pointer(
        repository,
        run_id,
        worktree=worktree,
        base=base,
        role="implement",
        manifest=manifest,
        write_paths=["declared"],
        extra={"repository_tree_snapshot": snapshot},
    )

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            gate="not-run",
            outcome="a run whose worktree vanished mid-flight",
            root=repository,
        )

    message = str(refusal.value)
    assert "TypeError" not in message
    assert "single acknowledgement" in message
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []
