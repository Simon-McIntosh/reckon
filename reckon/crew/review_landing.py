"""Measure an accepted delivery at the merge it will publish.

A plan can reserve landing for its coordinator through its locked ``landing``
decision with choice ``manual`` or ``coordinator``. This uses the decision store
so prose describing a manual-landing test cannot itself disable automation.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon.crew import landing_measure, merge, runs
from reckon.crew.node import CrewError


def manual_landing_reason(pointer: Mapping[str, Any]) -> str:
    """Read the explicit reservation of the landing decision."""
    from reckon import _store

    node = pointer.get("node") or {}
    plan = str(node.get("plan") or "")
    if not plan:
        return ""
    state, _ = _store.read_plan(str(pointer["project"]), plan, pointer.get("repo"))
    decision = (state.get("decisions") or {}).get("landing") or {}
    if str(decision.get("choice") or "").lower() in {"manual", "coordinator"}:
        return "the plan reserves landing for its coordinator: " + str(
            decision.get("rationale") or "the coordinator lands this run by hand"
        )
    return ""


def prepare_landing(
    pointer: Mapping[str, Any],
    head: str,
    gate_check: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check every owed review and the merged gate before promotion writes."""
    from reckon.crew import queue_landing

    project, run_id = str(pointer["project"]), str(pointer["run_id"])
    tree = Path(str(pointer.get("worktree") or pointer["repo"]))
    for tier in queue_landing._owed_review_tiers(pointer):
        review, stale = queue_landing._review_for_tier(
            project, run_id, head, tree, tier
        )
        if stale or not queue_landing._review_tier_is_complete(tier, review):
            raise CrewError(f"the stored {tier} review does not accept this head")
    repo = Path(str(pointer["repo"]))
    primary = merge._rev_parse(repo, "HEAD")
    candidate = merge.build_merge_commit(
        repo,
        primary,
        head,
        "merge: integrate the reviewed delivery",
        "Combine the accepted delivery with the pinned primary for gate verification.",
    )
    return replay_candidate(pointer, candidate, gate_check, config=config)


def replay_candidate(
    pointer: Mapping[str, Any],
    candidate: str,
    gate_check: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the recorded gate in the partition's own checkout of this candidate."""
    from reckon import flight
    from reckon.crew.promotion_gate import _merged_gate_finding, _rewrite_worktree_roots

    repo = Path(str(pointer["repo"]))
    project, run_id = str(pointer["project"]), str(pointer["run_id"])
    if config is None:
        config = flight.resolve(project, checkout_path=str(repo)).config
    landing = config.get("landing") or {}
    timeout = landing_measure.measure_timeout(config)
    partition = str(landing.get("measure_partition") or "all_debug")
    long_partition = str(landing.get("measure_long_partition") or "rigel")
    command, roots = _rewrite_worktree_roots(
        str(gate_check.get("command") or ""),
        roots=tuple(str(pointer.get(key) or "") for key in ("worktree", "repo")),
        checkout=Path("."),
    )
    if not command.strip():
        raise CrewError("the run records no gate command to measure at the merge")
    # A shell preserves the recorded command's operators and assignments. The
    # partition runner extracts the pinned revision on its own node, so a
    # node-local checkout is never passed to a different allocation as chdir.
    runner = landing_measure._partition_runner(
        landing_measure._RUN,
        repo=repo,
        base_tree=repo,
        base_sha=candidate,
        head_sha=candidate,
        partition=partition,
        long_partition=long_partition,
        test_files=(),
    )
    runner.partitioned = True
    directory = runs.run_dir(run_id) / "review-landing"
    log = directory / f"gate-{candidate}.log"
    arm = landing_measure._arm(
        ["sh", "-c", command],
        repo,
        log,
        runner,
        timeout,
    )
    verdict = (
        "passed"
        if arm.readable and arm.exit_status == 0
        else "failed"
        if arm.readable
        else "not-run"
    )
    reason = (
        "merged-head gate failed: " + ", ".join(arm.failure_ids)
        if arm.failure_ids
        else arm.unreadable_reason
        or ("the merged-head gate did not pass" if verdict != "passed" else "")
    )
    report = {
        "base_verdict": "passed",
        "integrated_revision": candidate,
        "integrated_verdict": verdict,
        "exit_status": arm.exit_status,
        "failure_ids": list(arm.failure_ids),
        "gate_command": str(gate_check["command"]),
        "replayed_command": shlex.join(arm.command),
        "worktree_roots_rewritten": list(roots),
        "measure_partition": landing_measure.partition_for_bound(
            partition, long_partition, timeout
        ),
        "log_path": str(log),
        "ran": arm.exit_status is not None,
        "ok": verdict == "passed",
        "reason": reason,
        "finding": _merged_gate_finding(
            "passed",
            verdict,
            integrated_revision=candidate,
            exit_status=arm.exit_status,
            reason=reason or None,
            failure_ids=arm.failure_ids,
            timed_out=arm.timed_out,
            replay_elapsed_seconds=arm.elapsed_seconds,
        ),
    }
    runs._write_json(directory / "gate-report.json", report)
    return report


def record_refusal(pointer: Mapping[str, Any], head: str, reason: str) -> None:
    """Publish one refusal through the landing obligation's existing writer."""
    from reckon.crew import queue_landing

    run_id = str(pointer["run_id"])
    remedy = shlex.join(
        ["reckon", "crew", "complete", "--run", run_id, "--gate", "passed", "--land"]
    )
    queue_landing._record_attempt(
        run_id,
        head=head,
        guard=reason,
        status="refused",
        remedy=remedy,
        previous=pointer.get("admission_landing"),
    )


def finish_landing(pointer: Mapping[str, Any], run_id: str) -> str:
    """Commit and publish the section declaration after the measured merge."""
    from reckon import _store, evidence
    from reckon.crew import queue_landing
    from reckon.crew.promotion_scope import _commit_landing_writes

    refusal = queue_landing._landing_beat(pointer, run_id)
    if refusal:
        return refusal
    node = pointer.get("node") or {}
    plan, section = str(node.get("plan") or ""), str(node.get("section") or "")
    if not plan or not section:
        return ""
    project, repo = str(pointer["project"]), Path(str(pointer["repo"]))
    plan_path = _store._resolve_html_file(project, plan, repo)
    docs_dir = _store._docs_dir_for_project(project, repo)
    if plan_path is None or docs_dir is None:
        return "the landing declaration's plan path could not be resolved"
    _commit_landing_writes(
        run_id=run_id,
        verdict="passed",
        checkout=repo,
        paths=[plan_path, evidence._evidence_record_path(docs_dir, plan)],
        subject="docs: record the measured delivery's landing",
        body="Record the section declaration after its reviewed head reaches the primary branch.",
    )
    merge._push_and_confirm(
        repo, "origin", merge._rev_parse(repo, "HEAD"), merge._current_branch(repo)
    )
    return ""
