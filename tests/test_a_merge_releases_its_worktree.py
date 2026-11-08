"""A merge releases the worktree its landed head leaves behind.

A run promoted on its own has its live pointer deleted, and the tree it worked
in stays held as a ``worktree-held`` obligation until someone sweeps it by
hand. ``crew merge --head`` lands and pushes the head, and the merge is the
command that knows the commit it wrote — so it is also the command that frees
every tree standing exactly at that commit, judged against the merge commit
rather than against a checkout's ``HEAD``.

Four cases pin the release: a promoted run landed separately is released and
named in the merge result; a tree carrying a commit the merged head lacks is
withheld, because releasing it would delete work no reader has; and a merge
refused before it pushes releases nothing; and a tree a ledger row retains as a
recoverable session's working directory is withheld, because it is the only
copy of the work that session can be resumed from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import ledger
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _write_origin_and_checkout,
)

SESSION = "session-a"
NODE = "release-node"


def _promoted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit_beyond_the_head: bool = False,
    retention_session: str = "",
) -> dict:
    """A run promoted with ``crew complete`` whose tree is left held.

    The tree lives at ``<root>/<session>/<node id>``, which is the path the
    ledger row names it by once the live pointer is gone.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    seed = _git(repository, "rev-parse", "HEAD")
    run_id = "r-20261108T030000000000-a-merge-releases-its-worktree"
    worktree = tmp_path / "worktrees" / SESSION / NODE
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), seed)
    (worktree / "reviewed.txt").write_text("the merged change\n", encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the merged change")
    head = _git(worktree, "rev-parse", "HEAD")
    if commit_beyond_the_head:
        (worktree / "beyond.txt").write_text(
            "a commit the head lacks\n", encoding="utf-8"
        )
        _git(worktree, "add", "beyond.txt")
        _git(worktree, "commit", "-q", "-m", "a commit beyond the merged head")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(f"node: {NODE}\nstatus: complete\n", encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "session": SESSION,
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-08T03:00:00Z",
            "base_sha": seed,
            "manifest_path": str(manifest),
            "node": {
                "id": NODE,
                "plan": PLAN,
                "section": "§9",
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    run = {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": run_id,
        "head": head,
    }
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--commit",
            head,
            "--outcome",
            "one call promoted the run; the merge lands it separately",
            "--checkout-path",
            str(repository),
            "--no-impl-change",
            "the fixture lands one file and moves no plan",
            "--gate-command",
            "true",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            str(gate_log),
        ],
    )
    assert result.exit_code == 0, result.output
    if retention_session:
        # A tree kept as the working directory of a single recoverable session
        # is named by the row's retention as well as by its node id, and it is
        # the one tree a release must never take.
        row = ledger.read_document(ledger.run_path(PROJECT, run_id, repository))
        row["worktree_retention"] = {
            "worktree": str(worktree),
            "session_id": retention_session,
        }
        ledger.write_run_file(PROJECT, row, repository)
    return run


def _merge(run: dict):
    return CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "merge",
            "--project",
            PROJECT,
            "--head",
            run["head"],
            "--message",
            "land the reviewed head",
            "--body",
            "The reviewed head reaches the primary branch.",
        ],
    )


def test_a_promoted_runs_worktree_is_released_by_a_separate_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge lands the head, frees the tree it leaves, and names it."""
    run = _promoted_run(tmp_path, monkeypatch)
    assert run["worktree"].is_dir()

    result = _merge(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    landed = _git(run["repository"], "rev-parse", "main")
    assert _git(run["origin"], "rev-parse", "main") == landed
    assert str(run["worktree"].resolve()) in report["release"]["released"], report[
        "release"
    ]
    assert report["release"]["withheld"] == [], report["release"]
    assert not run["worktree"].exists()


def test_a_tree_holding_a_commit_beyond_the_head_is_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree the merged head does not reach is kept, and named as withheld."""
    run = _promoted_run(tmp_path, monkeypatch, commit_beyond_the_head=True)

    result = _merge(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert str(run["worktree"].resolve()) not in report["release"]["released"], report
    withheld = {entry["worktree"] for entry in report["release"]["withheld"]}
    assert str(run["worktree"].resolve()) in withheld, report
    assert run["worktree"].is_dir()


def test_a_merge_refused_before_it_pushes_releases_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflicting head moves nothing, so the tree it would have freed stays."""
    run = _promoted_run(tmp_path, monkeypatch)
    # A peer commit on the primary branch touching the same path makes the
    # reviewed head conflict, so the merge is refused before any push.
    (run["repository"] / "reviewed.txt").write_text("a peer change\n", encoding="utf-8")
    _git(run["repository"], "add", "reviewed.txt")
    _git(run["repository"], "commit", "-q", "-m", "a peer change on the branch")
    origin_before = _git(run["origin"], "rev-parse", "main")

    result = _merge(run)

    assert result.exit_code != 0, result.output
    assert _git(run["origin"], "rev-parse", "main") == origin_before
    assert run["worktree"].is_dir()


def test_a_retained_worktree_is_withheld_and_its_retention_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree kept for a recoverable session survives the landed-head release.

    The promotion's own release withholds a retained tree, and the merge reads
    the same retention off the same record field, so a tree that is the only
    copy of a session's work is named rather than removed.
    """
    run = _promoted_run(tmp_path, monkeypatch, retention_session="session-b")

    result = _merge(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert str(run["worktree"].resolve()) not in report["release"]["released"], report
    entries = {entry["worktree"]: entry for entry in report["release"]["withheld"]}
    entry = entries[str(run["worktree"].resolve())]
    assert entry["retention"]["session_id"] == "session-b", entry
    assert "session-b" in entry["reason"], entry
    assert run["worktree"].is_dir()


def test_a_release_that_raises_does_not_refuse_the_landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch has moved and the push is confirmed; the release cannot undo it.

    A release step that raises after the push must leave the landing standing:
    the merge succeeds, and the result records the release as not judged rather
    than reporting a landed merge as a refused one.
    """
    run = _promoted_run(tmp_path, monkeypatch)
    from reckon.crew import merge as merge_module

    def _raise(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("the held-tree reader is unavailable")

    monkeypatch.setattr(merge_module, "release_landed_worktrees", _raise)

    result = _merge(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["pushed"] is True, report
    landed = _git(run["repository"], "rev-parse", "main")
    assert _git(run["origin"], "rev-parse", "main") == landed
    release = report["release"]
    assert release["judged"] is False, release
    assert "the held-tree reader is unavailable" in release["failed"], release
    assert release["released"] == [], release
