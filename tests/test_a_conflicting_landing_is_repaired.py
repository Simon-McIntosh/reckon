"""Concurrent deliveries meet at a real git merge and return conflicts for repair."""

from __future__ import annotations

import json

import pytest

from reckon.crew import merge, queue_landing, recovery, resumption, runs
from reckon.crew.recovery_repair_dispatch import (
    REPAIR_DISPATCH_FIELD,
    REPAIR_ROUNDS_FIELD,
)
from tests.helpers.git import git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _write_origin_and_checkout,
)
from tests.helpers.landing_repository import (
    _complete_land as complete_land,
)


def _complete_land(run):
    return complete_land(
        run,
        "--waive-unreviewed-promotion",
        "The fixture tests merge conflicts rather than independent review.",
    )


SOURCE = "functions.py"
ORIGINAL = (
    "def first():\n    return 'first original'\n\n\n"
    "# Keep independent function edits outside the same merge hunk.\n\n\n"
    "def second():\n    return 'second original'\n"
)


@pytest.fixture()
def deliveries(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repository / "docs")}))
    (repository / SOURCE).write_text(ORIGINAL)
    git(repository, "add", SOURCE)
    git(repository, "commit", "-q", "-m", "test: define independent functions")
    git(repository, "push", "-q", "origin", "main")
    base = git(repository, "rev-parse", "HEAD")
    resumed = []

    def resume(run_id, record, **kwargs):
        resumed.append((run_id, kwargs["advice"]))
        return {"turn": 1}

    monkeypatch.setattr(resumption, "_resume", resume)

    def deliver(name, old, replacement):
        run_id = "r-delivery-" + name
        worktree = tmp_path / name
        git(repository, "worktree", "add", "--detach", str(worktree), base)
        (worktree / SOURCE).write_text(ORIGINAL.replace(old, replacement))
        git(worktree, "add", SOURCE)
        git(worktree, "commit", "-q", "-m", "test: change function result")
        head = git(worktree, "rev-parse", "HEAD")
        manifest = tmp_path / (name + "-manifest.md")
        manifest.write_text(f"node: {name}\nstatus: complete\ncommits: {head}\n")
        log = tmp_path / (name + "-gate.log")
        log.write_text("# command: true\nEXIT=0\n")
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": PROJECT,
                "repo": str(repository),
                "worktree": str(worktree),
                "manifest_path": str(manifest),
                "base_sha": base,
                "launch": "in-harness",
                "role": "implement",
                "member": name,
                "backend": "native",
                "created_at": "2026-01-01T00:00:00Z",
                "node": {
                    "id": name,
                    "plan": PLAN,
                    "section": "section",
                    "role": "implement",
                    "time_budget": "30m",
                    "write_paths": [SOURCE],
                },
            },
        )
        return {
            "run_id": run_id,
            "head": head,
            "worktree": worktree,
            "repository": repository,
            "origin": origin,
            "gate_log": log,
            "manifest": manifest,
        }

    return deliver, resumed


def assert_clean(repository):
    assert git(repository, "status", "--porcelain") == ""
    content = git(repository, "show", "HEAD:" + SOURCE)
    assert "<<<<<<<" not in content
    assert "=======" not in content
    assert ">>>>>>>" not in content


def test_independent_functions_both_land(deliveries):
    deliver, resumed = deliveries
    first = deliver("first", "first original", "first delivered")
    second = deliver("second", "second original", "second delivered")
    for run in (first, second):
        result = _complete_land(run)
        assert result.exit_code == 0, result.output
        receipt = json.loads(result.output)
        assert receipt["steps"]["land"]["pushed"], receipt
        assert_clean(run["repository"])
    content = git(second["repository"], "show", "main:" + SOURCE)
    assert "first delivered" in content and "second delivered" in content
    assert git(second["origin"], "rev-parse", "main") == git(
        second["repository"], "rev-parse", "main"
    )
    assert resumed == []


def automatic_conflict(run, monkeypatch):
    """Drive the lander's conflict decision with a delivered, accepted review."""
    pointer = runs.read_pointer(run["run_id"])
    monkeypatch.setattr(runs, "list_live", lambda **kwargs: [pointer])
    monkeypatch.setattr(
        recovery,
        "classify_pointer",
        lambda *args, **kwargs: {
            "classification": "completed-but-unpromoted",
            "manifest_status": "complete",
            "manifest_commits": [run["head"]],
        },
    )
    monkeypatch.setattr(queue_landing, "_owed_review_tiers", lambda p: ["code"])
    monkeypatch.setattr(queue_landing, "_review_for_tier", lambda *a, **k: ({}, ""))
    monkeypatch.setattr(queue_landing, "_review_tier_is_complete", lambda *a: True)
    result = queue_landing.land_once()
    assert result["landed"] == [], result
    assert "conflict" in result["obligations"][0]["guard"], result
    return result


@pytest.mark.parametrize("route", ["manual", "automatic"])
def test_conflicting_delivery_opens_repair_then_lands(deliveries, monkeypatch, route):
    deliver, resumed = deliveries
    first = deliver("first", "first original", "first delivered")
    second = deliver("second", "first original", "second delivered")
    landed = _complete_land(first)
    assert landed.exit_code == 0, landed.output
    repository = second["repository"]
    primary = git(repository, "rev-parse", "HEAD")
    remote_before = git(second["origin"], "rev-parse", "main")
    if route == "manual":
        result = _complete_land(second)
        assert result.exit_code != 0, result.output
        assert "conflict" in result.output, result.output
    else:
        automatic_conflict(second, monkeypatch)
    assert git(second["origin"], "rev-parse", "main") == remote_before
    assert_clean(repository)
    pointer = runs.read_pointer(second["run_id"])
    outcome = pointer.get("merge_conflict", {})
    assert outcome.get("outcome") == "merge-conflict", pointer
    assert outcome["paths"] == [SOURCE]
    assert outcome["main_revision"] == primary
    assert pointer[REPAIR_ROUNDS_FIELD]["count"] == 1
    round_record = pointer[REPAIR_DISPATCH_FIELD]
    assert round_record["status"] == "resumed"
    assert len(resumed) == 1
    advice = resumed[0][1]
    assert SOURCE in advice and primary in advice
    assert "merge" in advice and "gate" in advice
    assert "both" in advice
    if route == "automatic":
        automatic_conflict(second, monkeypatch)
        assert len(resumed) == 1, "one conflict must not resume the same round twice"
    # Commit the resolved merge without ever writing conflict markers to the checkout.
    worktree = second["worktree"]
    content = ORIGINAL.replace("first original", "first delivered + second delivered")
    (worktree / SOURCE).write_text(content)
    git(worktree, "add", SOURCE)
    tree = git(worktree, "write-tree")
    repaired = merge._commit_tree(
        worktree,
        tree,
        [second["head"], primary],
        "fix: combine function changes\n\nPreserve both delivered results.\n",
    )
    git(worktree, "merge", "--ff-only", repaired)
    second["head"] = repaired
    second["manifest"].write_text(f"status: complete\ncommits: {repaired}\n")
    result = _complete_land(second)
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["steps"]["land"]["pushed"], receipt
    assert "first delivered + second delivered" in git(
        repository, "show", "main:" + SOURCE
    )
    assert git(second["origin"], "rev-parse", "main") == git(
        repository, "rev-parse", "main"
    )
    assert_clean(repository)
