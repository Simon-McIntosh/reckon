"""A worker's plan reads come from its own worktree.

A crew worker runs with ``RECKON_RUN_ID`` in its environment and its own git
worktree recorded on the live run pointer. Its MCP server inherits both. A plan
read that names no ``checkout_path`` must therefore resolve to the worker's own
worktree for the run's own project — the base revision the worker holds —
rather than the mounts-registered main checkout, whose copy is the coordinator's
live state. Reading the coordinator's version is how a worker came to write
against a version its own tree never had.

The same run scope reaches a plan write that would leave the worktree. The
registered write entry point already redirects a run's own project into the
worktree; the granular mutators below take no ``checkout_path`` and cannot, so
a run-scoped call to one is refused, naming the run and the path it would have
written, rather than returning ``ok``.

These tests build a synthetic repository and a synthetic run pointer; they
never touch the real ones. The main checkout's plan file is asserted
byte-identical after a run-scoped call that must not reach it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import reckon.crew.runs as crew_runs
from reckon import mcp as mcp_module
from reckon._store import new_plan_html, read_plan, write_plan

PROJECT = "proj"
OTHER_PROJECT = "other"
SLUG = "sample-plan"
RUN_ID = "r-20260101T000000000000-sample-node"
NODE = "sample-node"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_plan(root: Path, project: str, slug: str) -> Path:
    """Write one minimal plan into ``<root>/docs/plans/<slug>.html``."""
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{slug}.html"
    path.write_text(new_plan_html(project, slug), encoding="utf-8")
    return path


@pytest.fixture()
def run_scoped(tmp_path, monkeypatch):
    """A main checkout, a run worktree, a mounted sibling project and a live pointer."""
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    other_repo = tmp_path / "other_repo"
    main_plan = _write_plan(main, PROJECT, SLUG)
    worktree_plan = _write_plan(worktree, PROJECT, SLUG)
    other_main_plan = _write_plan(other_repo, OTHER_PROJECT, SLUG)

    # Give the worktree's copy a distinct version from the main checkout's, so a
    # read says which of the two it came from rather than matching either way.
    worktree_data, version = read_plan(PROJECT, SLUG, str(worktree))
    worktree_data["title"] = "worktree copy"
    write_plan(PROJECT, SLUG, worktree_data, version, root=str(worktree))

    mounts = tmp_path / "mounts.json"
    mounts.write_text(
        json.dumps(
            {
                PROJECT: str(main / "docs"),
                OTHER_PROJECT: str(other_repo / "docs"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))

    live = tmp_path / "live"
    live.mkdir()
    pointer = {
        "run_id": RUN_ID,
        "node": {"id": NODE},
        "project": PROJECT,
        "worktree": str(worktree),
        "repo": str(main),
    }
    (live / f"{RUN_ID}.json").write_text(json.dumps(pointer), encoding="utf-8")
    monkeypatch.setattr(crew_runs, "live_dir", lambda: live)
    monkeypatch.setenv("RECKON_RUN_ID", RUN_ID)

    return {
        "main": main,
        "worktree": worktree,
        "other_repo": other_repo,
        "main_plan": main_plan,
        "worktree_plan": worktree_plan,
        "other_plan": other_main_plan,
    }


def _read(*, project, slug=SLUG, checkout_path=None):
    return mcp_module._read_plan(
        project=project,
        slug=slug,
        checkout_path=checkout_path,
    )


def _version(root: Path, project: str, slug: str) -> int:
    _data, version = read_plan(project, slug, str(root))
    return version


def test_a_run_scoped_read_returns_the_worktree_version_not_main(run_scoped):
    fixture = run_scoped
    main_version = _version(fixture["main"], PROJECT, SLUG)
    worktree_version = _version(fixture["worktree"], PROJECT, SLUG)
    assert worktree_version != main_version

    result = _read(project=PROJECT)

    assert result["version"] == worktree_version, result
    assert "worktree copy" in str(result["data"].get("title", ""))


def test_a_run_scoped_read_of_another_project_comes_from_main(run_scoped):
    """A read of a project the run does not own has no worktree copy, so it is main."""
    fixture = run_scoped
    result = _read(project=OTHER_PROJECT)

    assert result["version"] == _version(fixture["other_repo"], OTHER_PROJECT, SLUG)


def test_a_coordinator_read_without_a_run_is_unchanged(run_scoped, monkeypatch):
    fixture = run_scoped
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)

    result = _read(project=PROJECT)

    assert result["version"] == _version(fixture["main"], PROJECT, SLUG)


def test_an_explicit_checkout_path_read_is_untouched_by_the_run_scope(run_scoped):
    """A caller that passes checkout_path keeps today's behaviour."""
    fixture = run_scoped
    result = _read(project=PROJECT, checkout_path=str(fixture["main"]))

    assert result["version"] == _version(fixture["main"], PROJECT, SLUG)


def test_a_run_scoped_granular_write_to_another_project_is_refused(run_scoped):
    """A plan-writing entry point with no checkout path cannot escape the worktree."""
    fixture = run_scoped
    before = _sha(fixture["other_plan"])

    result = mcp_module._set_status(
        OTHER_PROJECT,
        SLUG,
        "active",
        expected_version=_version(fixture["other_repo"], OTHER_PROJECT, SLUG),
    )

    assert result["ok"] is False, result
    assert result["error"] == "run_scoped_write"
    assert RUN_ID in result["message"]
    assert str(fixture["other_plan"]) in result["message"]
    assert _sha(fixture["other_plan"]) == before


def test_a_run_scoped_granular_write_to_the_runs_own_project_is_refused(run_scoped):
    """The granular mutators cannot land in the worktree, so their write fails loudly."""
    fixture = run_scoped
    before = _sha(fixture["main_plan"])

    result = mcp_module._set_status(
        PROJECT,
        SLUG,
        "active",
        expected_version=_version(fixture["worktree"], PROJECT, SLUG),
    )

    assert result["ok"] is False, result
    assert result["error"] == "run_scoped_write"
    assert RUN_ID in result["message"]
    assert str(fixture["main_plan"]) in result["message"]
    assert _sha(fixture["main_plan"]) == before


def test_a_coordinator_granular_write_without_a_run_is_unchanged(
    run_scoped, monkeypatch
):
    fixture = run_scoped
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    start = _version(fixture["main"], PROJECT, SLUG)

    result = mcp_module._set_status(PROJECT, SLUG, "active", expected_version=start)

    assert result["ok"] is True, result
    assert _version(fixture["main"], PROJECT, SLUG) == start + 1
