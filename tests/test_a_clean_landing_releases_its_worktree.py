"""A clean landing releases its worktree, and a gone one is not a held tree.

``crew complete --land`` judged the run's tree twice after the merge: once by
the landing's own release, which reads the commit the merge wrote, and again by
the release's audit, which reads the run's declared write paths off ``node``.
A promoted ledger row carries the node *id* there and the definition under
``node_definition``, so the audit raised, the release was recorded as raised,
and a tree the landing had already removed was reported withheld. Every hand
landing of a run whose manifest still named changed paths reported that, with
its reviewed head pushed.

A tree whose directory is already gone holds nothing to release, so it is
reported released rather than withheld; a withheld verdict named a tree no
command could free. And a tree holding a commit the primary lacks is still
withheld, with an obligation whose suggested sweep clears it once the commit
lands.

Every case synthesises its own repository, ledger and workspace under a
temporary config home.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

obligations_module = importlib.import_module("reckon.crew.obligations")
routing_module = importlib.import_module("reckon.crew.routing")

MARKER = "the reviewed head landed\n"
RUN_ID = "r-20261114T212249058819-a-clean-landing-releases-its-worktree"


def _landing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, commit_beyond_the_head: bool
) -> dict:
    """A promoted-shape run whose worktree holds one reviewed commit."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    seed = _git(repository, "rev-parse", "HEAD")
    # A dispatched tree sits under the managed root and is named for its node,
    # which is how a promoted row that lost its live pointer names the tree it
    # still holds, and what makes the row's own sweep reach it.
    root = routing_module._workspace_roots(repository)[0]
    worktree = root / "landing-session" / "node-a"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), seed)
    (worktree / "reviewed.txt").write_text(MARKER, encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "test: add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    if commit_beyond_the_head:
        (worktree / "beyond.txt").write_text("a commit the head lacks\n")
        _git(worktree, "add", "beyond.txt")
        _git(worktree, "commit", "-q", "-m", "test: a commit beyond the reviewed head")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\n",
        encoding="utf-8",
    )
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-14T21:22:49Z",
            "base_sha": seed,
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§20",
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": RUN_ID,
        "head": head,
        "gate_log": gate_log,
        "manifest": manifest,
    }


def test_a_clean_landing_releases_its_worktree_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pushed reviewed head frees the tree, and no duty survives it."""
    run = _landing_run(tmp_path, monkeypatch, commit_beyond_the_head=False)

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    report = json.loads(Path(json.loads(result.output)["record_path"]).read_text())
    assert report["merged"] is True, report
    landed = _git(run["repository"], "rev-parse", "main")
    # The marker the change introduced is on the primary branch and the remote,
    # so the release is judged against a head that really landed.
    assert MARKER.strip() in _git(run["repository"], "show", f"{landed}:reviewed.txt")
    assert _git(run["origin"], "rev-parse", "main") == landed
    assert report["release"]["worktree_released"] is True, report
    assert "worktree_withheld" not in report["release"], report
    assert not run["worktree"].is_dir()
    assert obligations_module._held_worktree_records(PROJECT) == {}


def test_a_landing_whose_worktree_is_already_gone_reports_it_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree removed before the landing holds nothing, so the landing is clean."""
    run = _landing_run(tmp_path, monkeypatch, commit_beyond_the_head=False)
    _git(run["repository"], "worktree", "remove", "--force", str(run["worktree"]))
    assert not run["worktree"].is_dir()
    assert str(run["worktree"]) not in _git(run["repository"], "worktree", "list")

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    report = json.loads(Path(json.loads(result.output)["record_path"]).read_text())
    assert report["release"]["worktree_released"] is True, report
    assert report["release"].get("worktree_absent") is True, report
    assert "worktree_withheld" not in report["release"], report
    assert obligations_module._held_worktree_records(PROJECT) == {}


def test_a_tree_holding_a_commit_the_primary_lacks_is_withheld_until_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree is kept while it holds unlanded work, and the duty's sweep frees it."""
    run = _landing_run(tmp_path, monkeypatch, commit_beyond_the_head=True)
    unreviewed = _git(run["repository"], "rev-parse", "HEAD")
    origin_before = _git(run["origin"], "rev-parse", "main")

    refused = _complete_land(run)

    assert refused.exit_code != 0, refused.output
    report = json.loads(Path(json.loads(refused.output)["record_path"]).read_text())
    assert run["worktree"].is_dir()
    assert report["worktree_released"] is False, report
    assert _git(run["origin"], "rev-parse", "main") == origin_before
    held = obligations_module._held_worktree_records(PROJECT)
    assert run["worktree"].resolve() in held, held
    assert str(held[run["worktree"].resolve()].get("run_id") or "") == RUN_ID

    # The commit the tree carries is landed, so the withheld verdict no longer
    # holds and the duty's own suggested sweep releases the tree.
    tree_head = _git(run["worktree"], "rev-parse", "HEAD")
    assert tree_head != unreviewed
    _git(run["repository"], "merge", "--no-ff", "-q", tree_head, "-m", "test: land it")
    swept = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(run["repository"]),
            "--project",
            PROJECT,
            "--run",
            RUN_ID,
            "--apply",
        ],
    )
    assert swept.exit_code == 0, swept.output
    assert not run["worktree"].is_dir()
    assert obligations_module._held_worktree_records(PROJECT) == {}
