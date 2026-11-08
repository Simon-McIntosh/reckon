"""Concurrent deliveries meet at a real git merge and return conflicts for repair."""

from __future__ import annotations

import json
import subprocess

import pytest

from reckon import crew_run_commands
from reckon.crew import (
    pending_landing,
    queue_landing,
    recovery,
    resumption,
    runs,
)
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
    citations = []
    if run.get("authored_head"):
        citations = ["--commit", run["head"]]
        run = {**run, "head": run["authored_head"]}
    return complete_land(
        run,
        *citations,
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


def deliver_repair_gate(run, *, evidence="passed"):
    """Capture an actual repair check and vary the evidence a caller supplies."""
    command = "git diff --check HEAD^ HEAD"
    if evidence == "failed":
        command = "git diff --exit-code HEAD^ HEAD"
    checked = subprocess.run(
        command, shell=True, cwd=run["worktree"], capture_output=True, text=True
    )
    assert checked.returncode == (1 if evidence == "failed" else 0)
    revision = run["authored_head"] if evidence == "stale" else run["head"]
    header = "" if evidence == "unbound" else f"# revision: {revision}\n"
    logged_command = "true" if evidence == "wrong-command" else command
    terminal = "" if evidence == "unfinished" else f"EXIT={checked.returncode}\n"
    run["gate_log"].write_text(
        header + f"# command: {logged_command}\n" + checked.stdout + terminal
    )
    fields = ""
    if evidence != "missing":
        log = str(run["gate_log"]) + (".absent" if evidence == "missing-log" else "")
        fields = f"tests: {command}\ntest_logs: {json.dumps([log])}\n"
    run["manifest"].write_text(
        f"status: complete\ncommits: {run['authored_head']} {run['head']}\n" + fields
    )


@pytest.mark.parametrize("route", ["manual", "automatic"])
def test_conflicting_delivery_opens_repair_then_lands(deliveries, monkeypatch, route):
    deliver, resumed = deliveries
    first = deliver("first", "first original", "first delivered")
    second = deliver("second", "first original", "second delivered")
    landed = _complete_land(first)
    assert landed.exit_code == 0, landed.output
    repository = second["repository"]
    primary = git(repository, "rev-parse", "HEAD")
    if route == "manual":
        result = _complete_land(second)
        assert result.exit_code != 0, result.output
        receipt = json.loads(result.output)
        assert receipt["steps"]["promote"]["ok"]
        assert receipt["steps"]["land"]["outcome"] == "merge-conflict"
    else:
        # The completed promotion survives a process ending before its merge.
        with monkeypatch.context() as interrupted:
            interrupted.setattr(
                crew_run_commands,
                "_land_a_promoted_run",
                lambda *a, **k: ({"ok": False, "interrupted": True}, 1),
            )
            result = _complete_land(second)
        assert result.exit_code != 0, result.output
        assert not runs.pointer_path(second["run_id"]).exists()
        result = queue_landing.land_once()
        assert result["interrupted_landings"][0]["status"] == "merge-conflict", result
    primary = git(repository, "rev-parse", "HEAD")
    assert "second delivered" not in git(second["origin"], "show", "main:" + SOURCE)
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
    assert pointer["pending_landing_repair"] is True
    pending_path = pending_landing._record_path(second["run_id"])
    assert pending_path.is_file()
    assert (
        pending_landing.finish_pending_landings()[0]["status"]
        == "awaiting-merge-repair"
    )
    assert len(resumed) == 1, "one conflict must not resume the same round twice"
    plan = repository / "docs" / "plans" / (PLAN + ".html")
    ledger = (
        repository / "docs" / "state" / PROJECT / "runs" / (second["run_id"] + ".json")
    )
    plan_before, promotion_before = plan.read_bytes(), ledger.read_bytes()
    # Resolve a real worker merge, including all primary files it brings in.
    worktree = second["worktree"]
    worker_merge = subprocess.run(
        ["git", "-C", str(worktree), "merge", "--no-commit", "--no-ff", primary],
        capture_output=True,
        text=True,
        check=False,
    )
    assert worker_merge.returncode == 1, worker_merge.stdout + worker_merge.stderr
    assert SOURCE in worker_merge.stdout
    assert_clean(repository)
    content = ORIGINAL.replace("first original", "first delivered + second delivered")
    (worktree / SOURCE).write_text(content)
    git(worktree, "add", SOURCE)
    git(
        worktree,
        "commit",
        "-q",
        "-m",
        "fix: combine function changes",
        "-m",
        "Preserve both delivered results and primary metadata.",
    )
    repaired = git(worktree, "rev-parse", "HEAD")
    second["authored_head"] = second["head"]
    second["head"] = repaired
    for evidence in (
        "missing",
        "missing-log",
        "failed",
        "stale",
        "unbound",
        "unfinished",
        "wrong-command",
    ):
        deliver_repair_gate(second, evidence=evidence)
        if route == "manual":
            refused = _complete_land(second)
            assert refused.exit_code != 0, (evidence, refused.output)
            row = json.loads(refused.output)["pending_landings"][0]
        else:
            row = queue_landing.land_once()["interrupted_landings"][0]
        assert row["status"] == "repair-gate-refused", (evidence, row)
        assert "gate" in row["error"], row
        assert pending_path.is_file()
        assert runs.pointer_path(second["run_id"]).is_file()
        assert git(second["origin"], "rev-parse", "main") == primary
        assert ledger.read_bytes() == promotion_before
        assert plan.read_bytes() == plan_before
        assert len(resumed) == 1
        assert_clean(repository)
    deliver_repair_gate(second)
    if route == "manual":
        result = _complete_land(second)
        assert result.exit_code == 0, result.output
        finished = json.loads(result.output)["pending_landings"][0]
    else:
        result = queue_landing.land_once()
        finished = result["interrupted_landings"][0]
    assert finished["status"] == "finished", result
    assert finished["head"] == repaired
    assert not pending_path.exists()
    assert not runs.pointer_path(second["run_id"]).exists()
    assert plan.read_bytes() == plan_before, (
        "finishing the merge must not write the plan"
    )
    assert ledger.read_bytes() == promotion_before, "the promotion record must stand"
    assert (
        git(repository, "rev-list", "--first-parent", "--count", primary + "..HEAD")
        == "1"
    )
    assert "first delivered + second delivered" in git(
        repository, "show", "main:" + SOURCE
    )
    assert git(second["origin"], "rev-parse", "main") == git(
        repository, "rev-parse", "main"
    )
    assert git(second["origin"], "merge-base", repaired, "main") == repaired
    assert_clean(repository)


def test_unpromoted_automatic_conflict_opens_repair(deliveries, monkeypatch):
    deliver, resumed = deliveries
    first = deliver("first", "first original", "first delivered")
    second = deliver("second", "first original", "second delivered")
    result = _complete_land(first)
    assert result.exit_code == 0, result.output
    automatic_conflict(second, monkeypatch)
    pointer = runs.read_pointer(second["run_id"])
    assert pointer["merge_conflict"]["outcome"] == "merge-conflict"
    assert pointer["merge_conflict"]["paths"] == [SOURCE]
    assert pointer[REPAIR_ROUNDS_FIELD]["count"] == 1
    assert len(resumed) == 1
