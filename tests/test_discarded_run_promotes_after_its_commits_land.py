"""A discarded run is promoted once its commits reach main through a follow-on.

A run can be discarded to free its claim before it is promoted: the coordinator
removes its live pointer, and a follow-on node is dispatched on the discarded
run's head. When the follow-on's merge carries the predecessor's commits into
main, the predecessor has landed work and no ledger row, because it was never
promoted and its citations predate the follow-on's base. The run directory the
discard leaves behind is enough to promote it after the fact: the supervisor
records the repository and worktree, the durable manifest names the commit the
run made, and the reconcile path completes from that record with no live
pointer.

This exercises the reconcile path from ``reckon/crew/promotion.py`` as imported
into this process. Repository, worktree and config home are all synthesised
under ``tmp_path``, so no real crew directory is read or written.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "sample"
PLAN = "plan-a"

# The declared negative control: with this set, completion refuses a run that
# has no live pointer — the pre-reconcile behaviour — so the discarded-run case
# must fail.
NEGATIVE_CONTROL = os.environ.get("DISCARDED_RUN_PROMOTION_NEGATIVE") == "1"


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


def _worktree(repository: Path, tmp_path: Path, name: str, revision: str) -> Path:
    worktree = tmp_path / "worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), revision],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return worktree


def _pointer(
    repository: Path,
    run_id: str,
    *,
    worktree: Path,
    base_sha: str,
    node_id: str,
    write_paths: list[str],
    manifest_path: str = "",
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": base_sha,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-10-01T06:00:00Z",
            "manifest_path": manifest_path,
            "node": {
                "id": node_id,
                "plan": PLAN,
                "section": "s1",
                "write_paths": write_paths,
            },
        },
    )


def test_a_discarded_run_promotes_after_its_commits_land(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The predecessor's run directory promotes it once its commit is in main."""
    if NEGATIVE_CONTROL:

        def _refuse_a_run_with_no_pointer(
            run_id: str, *, root: str | Path | None
        ) -> dict:
            if not pointer_path(run_id).exists():
                raise promotion.CrewError(f"no live run {run_id!r} to complete")
            return promotion.read_pointer(run_id)

        monkeypatch.setattr(
            promotion, "_read_pointer_or_rebuild", _refuse_a_run_with_no_pointer
        )

    predecessor = "r-predecessor"
    follow_on = "r-follow-on"
    base = _git(repository, "rev-parse", "HEAD")

    # The predecessor commits its work in its own worktree, at its base.
    pred_tree = _worktree(repository, tmp_path, "predecessor", base)
    (pred_tree / "predecessor.txt").write_text("predecessor work\n", encoding="utf-8")
    _git(pred_tree, "add", "predecessor.txt")
    _git(pred_tree, "commit", "-q", "-m", "test: predecessor deliverable")
    pred_commit = _git(pred_tree, "rev-parse", "HEAD")
    pred_dir = run_dir(predecessor)
    pred_dir.mkdir(parents=True)
    (pred_dir / "supervisor.json").write_text(
        json.dumps(
            {
                "run_id": predecessor,
                "repo": str(repository),
                "worktree": str(pred_tree),
                "fenced": False,
            }
        ),
        encoding="utf-8",
    )
    (pred_dir / "manifest.md").write_text(
        "node: node-predecessor\n"
        "status: complete\n"
        f"commits: {pred_commit}\n"
        "changed_paths: predecessor.txt\n"
        "tests: not applicable\n",
        encoding="utf-8",
    )
    _pointer(
        repository,
        predecessor,
        worktree=pred_tree,
        base_sha=base,
        node_id="node-predecessor",
        write_paths=["predecessor.txt"],
        manifest_path=str(pred_dir / "manifest.md"),
    )

    # The coordinator discards the predecessor to free its claim. Its commit is
    # not yet integrated, so the unintegrated worktree survives the discard.
    discard = crew.discard(predecessor)
    assert not pointer_path(predecessor).exists()
    assert discard["worktree_released"] is False
    assert pred_tree.is_dir()

    # A follow-on node is dispatched on the predecessor's head and its merge
    # carries the predecessor's commit into main.
    follow_tree = _worktree(repository, tmp_path, "follow-on", pred_commit)
    (follow_tree / "followon.txt").write_text("follow-on work\n", encoding="utf-8")
    _git(follow_tree, "add", "followon.txt")
    _git(follow_tree, "commit", "-q", "-m", "test: follow-on deliverable")
    follow_commit = _git(follow_tree, "rev-parse", "HEAD")
    _git(repository, "merge", "--ff-only", follow_commit)
    assert _git(repository, "merge-base", "--is-ancestor", pred_commit, "HEAD") == ""
    main_head = _git(repository, "rev-parse", "HEAD")
    assert main_head == follow_commit

    # The follow-on is promoted, writing its own ledger row.
    follow_dir = run_dir(follow_on)
    follow_dir.mkdir(parents=True)
    (follow_dir / "manifest.md").write_text(
        "node: node-follow-on\nstatus: complete\ntests: not applicable\n",
        encoding="utf-8",
    )
    _pointer(
        repository,
        follow_on,
        worktree=follow_tree,
        base_sha=pred_commit,
        node_id="node-follow-on",
        write_paths=["followon.txt"],
        manifest_path=str(follow_dir / "manifest.md"),
    )
    crew.complete(
        follow_on,
        gate="not-run",
        outcome="the follow-on landed",
        root=repository,
        commits=[follow_commit],
    )
    rows_after_follow_on = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows_after_follow_on] == [follow_on]

    # The discarded predecessor is promoted from its surviving run directory:
    # no live pointer, its commit now in main through the follow-on's merge.
    result = crew.complete(
        predecessor,
        gate="not-run",
        outcome="landed after the fact once its commit reached main",
        root=repository,
        commits=[pred_commit],
    )

    rows = ledger.runs(PROJECT, root=repository)
    predecessor_rows = [row for row in rows if row["run_id"] == predecessor]
    assert len(predecessor_rows) == 1
    assert predecessor_rows[0]["commits"] == [pred_commit]
    assert result["run_id"] == predecessor
    assert not pointer_path(predecessor).exists()

    # The follow-on's own row is unchanged by the predecessor's late promotion.
    follow_rows = [row for row in rows if row["run_id"] == follow_on]
    assert len(follow_rows) == 1
    assert follow_rows[0] == rows_after_follow_on[0]
