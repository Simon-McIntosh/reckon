"""A landing judges its worktree against the commit the landing wrote.

``crew complete --land`` promotes and records a release before it merges, so
that release reads the reviewed head off the primary branch and withholds the
tree as unintegrated. The release made after the merge frees the tree, and the
recorded post-merge reading is the one reported.

A run whose recorded checkout the landing did not move is judged the same way.
A worker's ``repo`` can name a checkout that observes the branch without being
advanced by the merge — a detached linked worktree, as one coordinator landing
recorded — and comparing the worker head with ``HEAD`` there withholds a tree
whose head the landing pushed. The release therefore compares the worker head
with the commit the landing wrote, in the checkout the landing wrote it to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

MARKER = "the reviewed head landed\n"


def _landed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    observer: bool = False,
    commit_beyond_the_head: bool = False,
) -> dict:
    """A promoted-shape run whose worktree holds one reviewed commit.

    With ``observer`` the pointer records a linked worktree detached at the seed
    commit instead of the project's registered checkout, which is the shape a
    real coordinator landing recorded. With ``commit_beyond_the_head`` the
    worktree gains a second commit the reviewed head does not carry.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    seed = _git(repository, "rev-parse", "HEAD")
    run_id = "r-20261108T000000000000-a-landed-head-releases-its-worktree"
    worktree = tmp_path / "worktrees" / "landed"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), seed)
    (worktree / "reviewed.txt").write_text(MARKER, encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    recorded_repo = repository
    if observer:
        recorded_repo = tmp_path / "observer"
        _git(repository, "worktree", "add", "--detach", str(recorded_repo), seed)
    if commit_beyond_the_head:
        (worktree / "beyond.txt").write_text("a commit the head lacks\n")
        _git(worktree, "add", "beyond.txt")
        _git(worktree, "commit", "-q", "-m", "a commit beyond the reviewed head")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(recorded_repo),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-08T00:00:00Z",
            "base_sha": seed,
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§6",
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "observer": recorded_repo,
        "worktree": worktree,
        "run_id": run_id,
        "head": head,
        "gate_log": gate_log,
    }


def test_a_landing_of_the_reviewed_head_releases_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-merge release is the one reported, and the tree is gone."""
    run = _landed_run(tmp_path, monkeypatch)

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["steps"]["land"]["ok"] is True, report
    assert report["steps"]["land"]["pushed"] is True
    # The marker the change introduced is on the primary branch and the remote.
    landed = _git(run["repository"], "rev-parse", "main")
    assert MARKER.strip() in _git(run["repository"], "show", "main:reviewed.txt")
    assert _git(run["origin"], "rev-parse", "main") == landed
    assert report["steps"]["release"]["worktree_released"] is True, report
    assert report["release"]["worktree_released"] is True, report
    assert not run["worktree"].exists()


def test_a_recorded_observer_checkout_still_releases_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree is judged against the landed commit, not the observer's HEAD."""
    run = _landed_run(tmp_path, monkeypatch, observer=True)
    observer_head = _git(run["observer"], "rev-parse", "HEAD")

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    landed = _git(run["repository"], "rev-parse", "main")
    assert _git(run["origin"], "rev-parse", "main") == landed
    # The observer really was left behind by the landing, so the release cannot
    # have passed by reading its HEAD.
    assert _git(run["observer"], "rev-parse", "HEAD") == observer_head
    assert observer_head != landed
    assert report["steps"]["release"]["worktree_released"] is True, report
    assert not run["worktree"].exists()


def test_a_worktree_holding_a_commit_beyond_the_head_is_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree carrying a commit the reviewed head lacks is kept, and nothing lands."""
    run = _landed_run(tmp_path, monkeypatch, commit_beyond_the_head=True)
    origin_before = _git(run["origin"], "rev-parse", "main")

    result = _complete_land(run)

    assert result.exit_code != 0, result.output
    report = json.loads(result.output)
    assert report["steps"]["land"]["ok"] is False, report
    assert report["worktree_released"] is False, report
    assert _git(run["origin"], "rev-parse", "main") == origin_before
    assert run["worktree"].is_dir()
