"""The pre-pick stages read each shared source once and keep the last good copy.

Four defects measured on 2026-10-07 delayed every dispatch before the picker's
own bound started. A project's repository was re-resolved, and git re-probed,
on every resolution in one process. A refused ledger index refresh fell back to
a full scan of the aggregate instead of the index the last good refresh had
published. An estimated-hours cache was stamped on the plan's own file and the
directory listings alone, so an in-place edit to a sibling plan left a stale
stamp. And the scratch survey's docstring said an unattributed directory is
never removed by age while the orphan sweep removes it by age.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import dispatch_sections, routing
from reckon.crew.node import TaskNode

PROJECT = "sample"

PLAN_TEMPLATE = (
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-slug" content="{slug}">'
    '<meta name="plan-effort-hours" content="{hours}">'
)
_RUN_TEMPLATE = {
    "member": "worker",
    "role": "implement",
    "outcome": "completed",
    "store_write": {"status": "written"},
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    return config_home


def _git_repo(path: Path) -> Path:
    """A throwaway git repository, enough for a git-common-dir probe."""
    path.mkdir(parents=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def _counting_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record every git probe a resolution makes."""
    from reckon.crew import node

    real = node._git_output
    calls: list[tuple] = []

    def counting(path, *args, **kwargs):
        calls.append((str(path), *args))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(node, "_git_output", counting)
    return calls


def _write_plan(docs: Path, relative: str, slug: str, hours: float) -> Path:
    path = docs / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLAN_TEMPLATE.format(slug=slug, hours=hours))
    return path


def _node(plan: str) -> TaskNode:
    return TaskNode(
        id="work",
        goal="Estimate the node",
        plan=plan,
        role="implement",
        spec_level="guided",
        done_when="The estimate is returned",
    )


# ── item 1: resolve the project repository once per process ──────────────────


def test_a_second_resolution_runs_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "repo")
    getattr(dispatch_sections, "_REPOSITORY_IDENTITIES", {}).clear()
    monkeypatch.setattr(
        dispatch_sections, "project_mount_repository", lambda project: repo
    )
    calls = _counting_git(monkeypatch)

    first = dispatch_sections.resolve_project_repository(PROJECT, repo)
    assert calls, "the first resolution must probe git for the repository identity"
    calls.clear()

    second = dispatch_sections.resolve_project_repository(PROJECT, repo)
    assert second == first
    assert calls == [], f"the second resolution ran git: {calls}"


# ── item 2: a refused refresh reads the last good index, with its age ─────────


def _place_aggregate(run_ids: list[str]) -> Path:
    path = ledger.ledger_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [dict(_RUN_TEMPLATE, run_id=run_id) for run_id in run_ids]
    path.write_text(
        json.dumps(
            {
                "data": {
                    "_version": 1,
                    "members": [],
                    "runs": rows,
                    "holds": [],
                }
            }
        )
    )
    return path


def test_a_refused_index_refresh_returns_rows_with_an_age_and_no_aggregate_scan(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _place_aggregate(["r-alpha", "r-beta"])
    current, _version = ledger.load(PROJECT)
    assert {row["run_id"] for row in current["runs"]} == {"r-alpha", "r-beta"}

    def refused(project, root):
        raise OSError("index refresh refused under a peer's write")

    scanned: list[object] = []

    def aggregate(project, root):
        scanned.append(project)
        return {"members": [], "runs": [], "holds": []}, 0

    monkeypatch.setattr(ledger, "_run_snapshot", refused)
    monkeypatch.setattr(ledger, "_load_aggregate", aggregate)
    time.sleep(0.01)

    stale, _version = ledger.load(PROJECT)
    assert {row["run_id"] for row in stale["runs"]} == {"r-alpha", "r-beta"}
    assert stale["index_age_seconds"] >= 0.0
    assert scanned == [], "a refused refresh opened the aggregate for a full scan"


# ── item 3: a sibling plan's metadata edit moves the estimate stamp ───────────


@pytest.fixture()
def docs_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "pick-cache"))
    repo = tmp_path / "repo"
    docs = repo / "docs"
    _write_plan(docs, "plans/alpha.html", "alpha", 3)
    _write_plan(docs, "plans/gamma.html", "gamma", None)
    _write_plan(docs, "plans/archive/retired.html", "retired", 9)
    return repo


def test_editing_a_sibling_plan_changes_the_stamp(
    docs_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scandir = os.scandir
    calls: list[object] = []

    def counting_scandir(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("path"))
        return real_scandir(*args, **kwargs)

    monkeypatch.setattr(os, "scandir", counting_scandir)

    assert routing._estimated_hours(docs_tree, PROJECT, _node("alpha")) == (
        3.0,
        "plan-fallback",
    )
    calls.clear()
    assert routing._estimated_hours(docs_tree, PROJECT, _node("alpha")) == (
        3.0,
        "plan-fallback",
    )
    assert calls == [], f"an unchanged tree walked directories: {calls}"

    # A sibling plan's metadata changes what the resolve summarised, so the
    # cached figure must be rebuilt even though the named plan is untouched.
    _write_plan(docs_tree / "docs", "plans/gamma.html", "gamma", 6)
    assert routing._estimated_hours(docs_tree, PROJECT, _node("alpha")) == (
        3.0,
        "plan-fallback",
    )
    assert calls, "the sibling plan's edit did not move the estimate stamp"


# ── item 4: the survey docstring states the age rule the sweep applies ────────


def test_the_scratch_survey_docstring_states_the_orphan_sweep_age_rule() -> None:
    doc = inspect.getdoc(routing.garbage_collect_scratch) or ""
    match = re.search(r"SCRATCH_GRACE_SECONDS\s+\((\d+) seconds\)", doc)
    assert match is not None, "the survey docstring no longer states the age rule"
    assert int(match.group(1)) == routing.SCRATCH_GRACE_SECONDS


def test_the_orphan_sweep_uses_the_stated_grace() -> None:
    """The sweep's own age rule is the one the survey docstring now states."""
    source = inspect.getsource(routing.garbage_collect_orphan_scratch)
    assert "SCRATCH_GRACE_SECONDS" in source
    assert routing.SCRATCH_GRACE_SECONDS > 0


# ── the timed driver: a second dispatch pays under half a second ──────────────


def _pre_pick_stages(repo: Path, project: str) -> None:
    dispatch_sections.resolve_project_repository(project, repo)
    ledger.picker_runs(project)
    routing._estimated_hours(repo, project, _node("alpha"))


def test_the_second_dispatch_pays_under_half_a_second_for_its_pre_pick_stages(
    tmp_path: Path, home: Path, docs_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _place_aggregate(["r-alpha"])
    getattr(dispatch_sections, "_REPOSITORY_IDENTITIES", {}).clear()
    monkeypatch.setattr(
        dispatch_sections, "project_mount_repository", lambda project: docs_tree
    )

    _pre_pick_stages(docs_tree, PROJECT)  # warm every cache

    start = time.perf_counter()
    _pre_pick_stages(docs_tree, PROJECT)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"second dispatch's pre-pick stages took {elapsed:.3f}s"
