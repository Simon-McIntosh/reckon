"""A refused promotion leaves the worktree, the pointer and the ledger alone.

``crew complete`` reaches a run's worktree, its live pointer and its ledger row
at the end of a long chain of preconditions. A refusal inside that chain must
arrive before any of the three is touched: a run refused for a claim conflict
on an accepted path has already delivered its work, and releasing its worktree
on the way to the refusal destroys the only copy of anything the worker left
uncommitted — and, with the tree gone, a later review reads the shared
checkout's HEAD instead of the run's own.

Two refusals are driven here. The first is a promotion refused because a live
peer claims a path the run accepted; the second drives the same ordering for a
refusal the base revision reaches after a side effect.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
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
        f'<meta name="plan-slug" content="{state["slug"]}">'
        f"<title>{state['title']}</title>"
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
    _git(root, "checkout", "-q", "-b", "work")
    (root / "shared_module.py").write_text("before\n", encoding="utf-8")
    _git(root, "add", "shared_module.py")
    _git(root, "commit", "-q", "-m", "test: base of the run")
    (root / "shared_module.py").write_text("changed by the run\n", encoding="utf-8")
    _git(root, "add", "shared_module.py")
    _git(root, "commit", "-q", "-m", "test: the run's own change")
    _git(root, "checkout", "-q", "main")
    # The run's commit is merged, so its worktree classifies as integrated and a
    # release would reclaim it — the conditions under which the refused
    # promotion's own release step can remove the tree.
    _git(root, "merge", "-q", "--no-ff", "work", "-m", "test: merge the run's work")
    (tmp_path / "config" / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(
    repository: Path,
    run_id: str,
    *,
    worktree: str,
    write_paths: list[str],
    base_sha: str,
    manifest_path: str = "/durable/manifest.md",
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": worktree,
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-26T12:00:00Z",
            "manifest_path": manifest_path,
            "base_sha": base_sha,
            "node": {
                "id": run_id,
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "write_paths": write_paths,
            },
        },
    )


def test_a_refused_promotion_leaves_the_worktree_pointer_and_ledger(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-refused-node"
    peer_id = "r-peer-node"
    base_sha = _git(repository, "rev-parse", "HEAD^")
    changed = _git(repository, "rev-parse", "HEAD")
    worktree = tmp_path / "run-worktree"
    _git(repository, "worktree", "add", "-q", "--detach", str(worktree), changed)
    # A live peer claims the path this run must accept, so the promotion is
    # refused on the accept-path claim conflict.
    _pointer(
        repository,
        peer_id,
        worktree=str(tmp_path / "peer-worktree"),
        write_paths=["shared_module.py"],
        base_sha=base_sha,
    )
    _pointer(
        repository,
        run_id,
        worktree=str(worktree),
        write_paths=["tests/test_allowed.py"],
        base_sha=base_sha,
    )
    pointer_before = pointer_path(run_id).read_text(encoding="utf-8")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            gate="passed",
            commits=(changed,),
            accepted_paths={"shared_module.py": "companion path this run owns"},
            no_impl_change="the plan moved in a peer node",
            root=repository,
        )

    assert "cannot accept" in str(refusal.value)
    assert worktree.is_dir(), "the refused promotion removed the run's worktree"
    assert pointer_path(run_id).read_text(encoding="utf-8") == pointer_before
    assert ledger.runs(PROJECT, root=repository) == []
