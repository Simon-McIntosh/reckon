"""A worker's plan write lands in that run's own worktree.

A crew worker runs with ``RECKON_RUN_ID`` in its environment and its own git
worktree recorded on the live run pointer. Its MCP server inherits both. A plan
write that names no ``checkout_path`` must therefore land in the worker's own
worktree — never the mounts-registered main checkout, whose write goes live for
every session through the editable install and bypasses the merge, the audit
and review.

These tests build a synthetic repository and a synthetic run pointer; they
never touch the real ones. The main checkout's plan file is asserted
byte-identical after a run-scoped write, which is the fact that matters: a
write that reached main would change its bytes while still reporting success.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import reckon.crew.runs as crew_runs
from reckon import mcp as mcp_module
from reckon._store import new_plan_html, read_plan

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


def _edit(*, project, title, expected_version, checkout_path=None, slug=SLUG):
    return mcp_module._edit_plan(
        project=project,
        slug=slug,
        ops=[{"op": "set", "path": "title", "value": title}],
        expected_version=expected_version,
        checkout_path=checkout_path,
    )


def _version(root: Path, project: str, slug: str) -> int:
    _data, version = read_plan(project, slug, str(root))
    return version


def test_run_scoped_write_lands_in_the_worktree_and_leaves_main_identical(
    run_scoped,
):
    fixture = run_scoped
    before = _sha(fixture["main_plan"])
    start_version = _version(fixture["worktree"], PROJECT, SLUG)

    result = _edit(
        project=PROJECT,
        title="written by the worker",
        expected_version=start_version,
    )

    assert result["ok"] is True, result
    written = Path(result["path"])
    assert fixture["worktree"] in written.parents
    assert result["new_version"] == start_version + 1
    assert _version(fixture["worktree"], PROJECT, SLUG) == start_version + 1
    assert _sha(fixture["main_plan"]) == before
    assert "written by the worker" in fixture["worktree_plan"].read_text()


def test_write_to_another_projects_plan_is_refused(run_scoped):
    fixture = run_scoped
    before = _sha(fixture["other_plan"])

    result = _edit(
        project=OTHER_PROJECT,
        title="cross-project write",
        expected_version=_version(fixture["other_repo"], OTHER_PROJECT, SLUG),
    )

    assert result["ok"] is False, result
    assert result["error"] == "run_scoped_write"
    assert RUN_ID in result["message"]
    assert str(fixture["other_plan"]) in result["message"]
    assert _sha(fixture["other_plan"]) == before


def test_coordinator_write_without_a_run_is_unchanged(run_scoped, monkeypatch):
    fixture = run_scoped
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    before = _sha(fixture["worktree_plan"])

    result = _edit(
        project=PROJECT,
        title="written by the coordinator",
        expected_version=_version(fixture["main"], PROJECT, SLUG),
    )

    assert result["ok"] is True, result
    written = Path(result["path"])
    assert fixture["main"] in written.parents
    assert fixture["worktree"] not in written.parents
    assert "written by the coordinator" in fixture["main_plan"].read_text()
    assert _sha(fixture["worktree_plan"]) == before


def test_explicit_checkout_path_is_untouched_by_the_run_scope(run_scoped):
    """A caller that passes checkout_path keeps today's behaviour."""
    fixture = run_scoped
    result = _edit(
        project=PROJECT,
        title="explicit main write",
        checkout_path=str(fixture["main"]),
        expected_version=_version(fixture["main"], PROJECT, SLUG),
    )
    assert result["ok"] is True, result
    assert fixture["main"] in Path(result["path"]).parents
