"""The pre-pick stages read each shared source once and keep the last good copy.

Four defects measured on 2026-10-07 delayed every dispatch before the picker's
own bound started. A project's repository was re-resolved, and git re-probed,
on every resolution in one process. A refused ledger index refresh fell back to
a full scan of the aggregate instead of the index the last good refresh had
published. An estimated-hours cache was stamped on the plan's own file and the
directory listings alone, so an in-place edit to a sibling plan left a stale
stamp. And the scratch survey's docstring said an unattributed directory is
never removed by age while the orphan sweep removes it by age.

Each guard here measures the behaviour it names rather than a claim about it: a
worktree path resolved before its mount pays one git probe, not two; the orphan
sweep's age boundary is exercised just inside and just outside the figure the
survey docstring states; and a second dispatch runs no git command.
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
    """A throwaway git repository with one commit, enough for a common-dir probe."""
    path.mkdir(parents=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "README").write_text("repo\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "README"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-qm", "init"],
        check=True,
        capture_output=True,
    )
    return path


def _linked_worktree(repo: Path, path: Path) -> Path:
    """A linked worktree: a second spelling of one repository's common dir."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(path)],
        check=True,
        capture_output=True,
    )
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


# ── item 1: the repository identity is probed once per repository ─────────────


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


def test_a_worktree_and_its_mount_probe_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving a worktree path and then its mount costs one git probe."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _linked_worktree(repo, tmp_path / "linked")
    getattr(dispatch_sections, "_REPOSITORY_IDENTITIES", {}).clear()
    calls = _counting_git(monkeypatch)

    worktree_identity = dispatch_sections.repository_identity_once(worktree)
    assert len(calls) == 1, f"the worktree probe ran git {len(calls)} times"
    calls.clear()

    mount_identity = dispatch_sections.repository_identity_once(repo)
    assert mount_identity == worktree_identity
    assert calls == [], f"the mount after the worktree re-probed git: {calls}"


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


def _stated_grace_seconds() -> int:
    """The grace figure the survey docstring quotes, as an integer."""
    doc = inspect.getdoc(routing.garbage_collect_scratch) or ""
    match = re.search(r"SCRATCH_GRACE_SECONDS\s+\((\d+) seconds\)", doc)
    assert match is not None, "the survey docstring no longer states the age rule"
    return int(match.group(1))


def test_the_scratch_survey_docstring_states_the_orphan_sweep_age_rule() -> None:
    assert _stated_grace_seconds() == routing.SCRATCH_GRACE_SECONDS


def test_the_orphan_sweep_removes_by_the_stated_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep's age boundary is the figure the survey docstring states."""
    stated = _stated_grace_seconds()
    assert stated == routing.SCRATCH_GRACE_SECONDS

    root = (tmp_path / "scratch").resolve()
    root.mkdir()
    inside = root / "inside-grace"
    outside = root / "outside-grace"
    inside.mkdir()
    outside.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))

    now = time.time()
    ages = {
        "inside-grace": float(stated) - 1.0,
        "outside-grace": float(stated) + 1.0,
    }
    monkeypatch.setattr(routing, "_scratch_ctime", lambda path: now - ages[path.name])

    result = routing.garbage_collect_orphan_scratch(apply=True, now=now)
    removed = {Path(path).name for path in result["removed"]}
    assert "inside-grace" not in removed, (
        f"the sweep removed a directory {ages['inside-grace']:.0f}s inside the "
        f"stated {stated}s grace"
    )
    assert "outside-grace" in removed, (
        f"the sweep kept a directory {ages['outside-grace']:.0f}s past the "
        f"stated {stated}s grace"
    )


# ── the timed driver: a second dispatch runs no git ───────────────────────────


def _pre_pick_stages(repo: Path, project: str) -> None:
    dispatch_sections.resolve_project_repository(project, repo)
    ledger.picker_runs(project)
    routing._estimated_hours(repo, project, _node("alpha"))


def test_the_second_dispatch_runs_no_git(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm process pays one probe for a worktree and its mount, then none."""
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "pick-cache"))
    repo = _git_repo(tmp_path / "repo")
    _write_plan(repo / "docs", "plans/alpha.html", "alpha", 3)
    subprocess.run(
        ["git", "-C", str(repo), "add", "docs"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "plans"],
        check=True,
        capture_output=True,
    )
    worktree = _linked_worktree(repo, tmp_path / "linked")
    _place_aggregate(["r-alpha"])
    getattr(dispatch_sections, "_REPOSITORY_IDENTITIES", {}).clear()
    monkeypatch.setattr(
        dispatch_sections, "project_mount_repository", lambda project: repo
    )
    calls = _counting_git(monkeypatch)

    _pre_pick_stages(worktree, PROJECT)  # first dispatch: worktree, then its mount
    assert len(calls) == 1, (
        f"the worktree and its mount cost {len(calls)} git probes, not one"
    )
    calls.clear()

    _pre_pick_stages(worktree, PROJECT)  # second dispatch: nothing left to probe
    assert calls == [], f"the second dispatch ran git: {calls}"
