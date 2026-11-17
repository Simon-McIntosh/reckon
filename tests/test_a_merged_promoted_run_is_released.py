"""Merged promotions release safely through the unattended landing pass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, ledger
from reckon.crew import obligations, pending_landing, recovery, runs
from tests.helpers.git import git
from tests.helpers.landing_repository import PLAN, PROJECT, _write_origin_and_checkout


@pytest.fixture()
def cohort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    _origin, repo = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    plan = repo / "docs" / "plans" / f"{PLAN}.html"
    plan.write_text(
        _plan_html.write_state(
            plan.read_text(),
            {
                "section_declarations": {"s1": "implementable", "s2": "implementable"},
                "section_depends_on": {"s2": [f"{PLAN}#s1"]},
            },
        )
    )
    git(repo, "add", str(plan))
    git(repo, "commit", "-qm", "test: declare dependent sections")
    (repo / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    base = git(repo, "rev-parse", "HEAD")
    members = {}
    for name in ("clean", "dirty", "unresolved", "unmerged"):
        run_id = f"r-merged-{name}"
        tree = tmp_path / "worktrees" / name
        git(repo, "worktree", "add", "--detach", str(tree), base)
        (tree / f"{name}.txt").write_text(f"delivered {name}\n")
        git(tree, "add", f"{name}.txt")
        git(tree, "commit", "-qm", f"feat: deliver {name}")
        head = git(tree, "rev-parse", "HEAD")
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True)
        manifest = directory / "manifest.md"
        commits = [head, "missing-commit"] if name == "unresolved" else [head]
        manifest.write_text(
            f"status: complete\ncommits: {json.dumps(commits)}\nblockers: none\n"
        )
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(tree),
            "base_sha": base,
            "phase": "complete",
            "session": "owner",
            "role": "implement",
            "launch": "in-harness",
            "manifest_path": str(manifest),
            "node": {"id": name, "time_budget": "20m", "write_paths": [f"{name}.txt"]},
        }
        if name == "clean":
            pointer["node"].update(plan=PLAN, section="s1")
        runs._write_json(runs.pointer_path(run_id), pointer)
        ledger.write_run_file(
            PROJECT,
            {**pointer, "commits": [head], "promoted_revision": head, "gate": "passed"},
            repo,
        )
        if name != "unmerged":
            git(repo, "merge", "--no-ff", "-m", f"merge: deliver {name}", head)
        members[name] = {"pointer": pointer, "tree": tree, "head": head}
    (members["dirty"]["tree"] / "uncommitted.txt").write_text("keep this work\n")
    git(repo, "push", "-q", "origin", "main")
    waiter = {
        "run_id": "r-waiter",
        "project": PROJECT,
        "repo": str(repo),
        "session": "owner",
        "phase": "awaiting-upstream",
        "waits": [f"{PLAN}#s1"],
        "node": {"id": "waiter", "plan": PLAN, "section": "s2", "time_budget": "20m"},
    }
    runs._write_json(runs.pointer_path("r-waiter"), waiter)
    assert runs.read_pointer("r-waiter")["phase"] == "awaiting-upstream"
    return {"repo": repo, "members": members}


def test_merged_clean_run_releases_workspace_pointer_and_waiter(cohort: dict) -> None:
    clean = cohort["members"]["clean"]
    run_id = clean["pointer"]["run_id"]
    result = pending_landing.finish_pending_landings()
    assert not runs.pointer_path(run_id).exists(), result
    assert not clean["tree"].exists()
    row = json.loads(ledger.run_path(PROJECT, run_id, cohort["repo"]).read_text())
    assert row["release"]["worktree_released"] is True
    assert row["release"].get("worktree_absent") is not True
    assert runs.read_pointer("r-waiter")["phase"] == "queued"
    state, _version = _store.read_plan(PROJECT, PLAN, cohort["repo"])
    assert state["section_declarations"]["s1"] == "done"
    assert (cohort["repo"] / "clean.txt").read_text() == "delivered clean\n"
    assert run_id not in {
        row["run_id"] for row in pending_landing.finish_pending_landings()
    }
    assert (
        json.loads(ledger.run_path(PROJECT, run_id, cohort["repo"]).read_text())[
            "release"
        ]
        == row["release"]
    )


@pytest.mark.parametrize(
    "name,cause", [("dirty", "uncommitted"), ("unresolved", "missing-commit")]
)
def test_unsafe_merged_run_is_retained_as_one_obligation(
    cohort: dict, name: str, cause: str
) -> None:
    member = cohort["members"][name]
    pending_landing.finish_pending_landings()
    pointer = runs.read_pointer(member["pointer"]["run_id"])
    assert member["tree"].exists()
    snapshot = recovery.classify_pointer(pointer)
    assert snapshot["classification"] == "unreadable", snapshot
    assert cause in snapshot["detail"]
    view = obligations.obligations(PROJECT, "owner")
    items = [
        item for item in view["obligations"] if item.get("run_id") == pointer["run_id"]
    ]
    assert len(items) == 1, items
    assert cause in items[0]["reason"], items


def test_unmerged_run_is_untouched(cohort: dict) -> None:
    member = cohort["members"]["unmerged"]
    path = runs.pointer_path(member["pointer"]["run_id"])
    before = path.read_bytes()
    pending_landing.finish_pending_landings()
    assert path.read_bytes() == before
    assert member["tree"].exists()
    row = json.loads(
        ledger.run_path(
            PROJECT, member["pointer"]["run_id"], cohort["repo"]
        ).read_text()
    )
    assert "release" not in row
