# ruff: noqa: I001, UP035
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from reckon import review_tiers
from reckon.crew import review as review_module
from reckon.crew.node import (
    CrewError,
)
from reckon.crew.runs import (
    list_live,
)



# The score a complete review must reach for its acceptance to promote
# the reviewed run without a coordinator. The total a review can score is the
# schema dimensions at ``REVIEW_MAX_SCORE`` each, and the floor is nine tenths of
# that, so the accepting branch over a stored record whose total merely parses
# is what this exists to prevent.
REVIEW_ACCEPTANCE_FLOOR = (
    len(review_module.REVIEW_DIMENSIONS) * review_module.REVIEW_MAX_SCORE * 9 // 10
)


def _review_accepts_promotion(review: Mapping[str, Any] | None) -> bool:
    """Whether a stored review is clean enough to promote the run it read.

    Clean means complete — every dimension scored and a total parsed — with a
    total at or above the floor and no finding at all. Findings are read from
    the record rather than from the promotion-time severity filter: a review
    that carries any finding returns to the coordinator, so one finding of any
    severity is enough to withhold acceptance.
    """
    if not _review_is_complete(review):
        return False
    total = review.get("total")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return False
    if int(total) < REVIEW_ACCEPTANCE_FLOOR:
        return False
    findings = review.get("findings")
    return not (isinstance(findings, Sequence) and len(findings))


def _recorded_gate_passed(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The run's recorded gate check when it passed and its named commits.

    The gate a run records is its own manifest's evidence — the command under
    ``tests``, the log under ``test_logs`` and the ``EXIT=`` line the log
    carries — read through the same resolver a promotion fills its evidence
    from. A command with no exit status of zero, or no command at all, is not a
    passing gate and acceptance stands down for it. The resolved check and the
    commits the manifest names are returned together, so the caller can both
    re-run the same command at the merged head and require those commits to be
    on the branch that head names.
    """
    from reckon.crew import promotion as promotion_module

    gate_check, commits = promotion_module._default_gate_evidence_from_manifest(
        record, verdict="passed", gate_check=None, commits=()
    )
    if not str(gate_check.get("command") or "").strip():
        return None, ()
    if gate_check.get("exit_status") != 0:
        return None, ()
    return gate_check, tuple(commits)


def _commits_missing_from(repository: Path, commits: Sequence[str]) -> list[str]:
    """The named commits that are not ancestors of the repository's HEAD.

    A run's work is only landable once a coordinator has merged it onto the
    branch the checkout carries; a run reaching acceptance straight after its
    review has not been merged, so its commits are absent from the primary
    branch and a gate re-run here would verify a tree the change is not in.
    ``git merge-base --is-ancestor`` answers with status 0 for an ancestor, 1
    for a commit present but not an ancestor, and any other status for an
    instrument fault — an unresolvable object, a checkout that is not a work
    tree, git itself failing. Status 1 is the only one read as "not merged";
    every other failure raises, because an instrument that cannot answer must
    not be reported as the answer "not an ancestor".
    """
    missing: list[str] = []
    for commit in commits:
        sha = str(commit).strip()
        if not sha:
            continue
        probe = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                sha,
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            continue
        if probe.returncode == 1:
            missing.append(sha)
            continue
        raise CrewError(
            f"git could not decide whether {sha} is an ancestor of HEAD in "
            f"{repository}: exit {probe.returncode}: "
            f"{(probe.stderr or probe.stdout).strip()}"
        )
    return missing


def accept_clean_review(
    pointer: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Promote the reviewed run when its stored review is clean.

    When a review run completes with a stored record whose complete total
    is at least the acceptance floor and which carries no finding, the reviewed
    run is promoted without a coordinator: the commits its manifest names must
    already be ancestors of the repository's head, the run's own recorded gate
    must already have passed, the gate is re-run against the repository's
    current head with the run merged, and on all of those passing the run is
    promoted with ``promoted_by: review-acceptance`` on its ledger row.
    Anything short of that returns to the coordinator exactly as
    before, so this is the accepting branch beside the refusal that stops a
    promotion without a clean review rather than a bypass of it. Acceptance
    never merges: an unmerged run stands down and names the commits it found
    off the branch.

    None is returned when the run is not acceptance-eligible at all — no stored
    review, or a review that is incomplete, below the floor, or carrying a
    finding — because those runs are the coordinator's and this handler has
    nothing to add. A dict is returned whenever the run was eligible and an
    outcome was reached, so the sweep records why a refusal did not promote.
    """
    run_id = str(pointer.get("run_id") or "")
    project = str(pointer.get("project") or "")
    if not run_id or not project:
        return None
    head, tree = _review_head_and_tree(pointer)
    try:
        review, _reviewed_head = select_review_for_head(
            project, run_id, head, tree=tree
        )
    except (OSError, ValueError):
        review = None
    if not _review_accepts_promotion(review):
        return None
    checkout_value = str(pointer.get("repo") or "").strip()
    checkout = Path(checkout_value).expanduser() if checkout_value else None
    if checkout is None or not checkout.is_dir():
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": "the run names no repository to verify the merged gate against",
        }
    gate_check, commits = _recorded_gate_passed(pointer)
    if gate_check is None:
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": "the run records no passing gate to re-run at the merged head",
        }
    # The run's work must already be on the branch this checkout carries. A run
    # reaching acceptance straight after its review has not been merged, so a
    # gate re-run here would verify a tree the change is not in and record a
    # promotion whose commits never landed. Only a coordinator merges; this
    # handler stands down and says which commits are missing.
    missing = _commits_missing_from(checkout, commits)
    if missing:
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": (
                "the run's commits are not on the primary branch yet; a "
                "coordinator merges it: " + ", ".join(sha[:12] for sha in missing)
            ),
            "unmerged_commits": list(missing),
        }
    from reckon.crew import promotion as promotion_module

    worktree = str(pointer.get("worktree") or "").strip()
    report = promotion_module.rerun_gate_at_integrated_revision(
        repository=checkout,
        gate_check=gate_check,
        base_verdict="passed",
        integrated_revision="HEAD",
        worktree_roots=(worktree,) if worktree else (),
    )
    if str(report.get("integrated_verdict")) != "passed":
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": str(
                report.get("finding")
                or report.get("reason")
                or "the integrated gate did not pass at the merged head"
            ),
            "gate_report": report,
        }
    try:
        result = promotion_module.complete(
            run_id,
            gate="passed",
            gate_check=gate_check,
            root=checkout,
            promoted_by="review-acceptance",
        )
    except (CrewError, OSError) as refusal:
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": str(refusal),
            "gate_report": report,
        }
    return {
        "run_id": run_id,
        "accepted": True,
        "promoted": True,
        "review": {
            "total": review.get("total") if review else None,
            "findings": 0,
        },
        "gate_report": report,
        "ledger": {
            "path": str(result.get("ledger_path") or ""),
            "promoted_by": (
                str((result.get("record") or {}).get("promoted_by") or "")
                if isinstance(result.get("record"), Mapping)
                else "review-acceptance"
            ),
        },
    }


def dispatch_awaiting_reviews(
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Dispatch the review every scoring run composes, and report each outcome.

    This is the reflex's entry point: called on a sweep, it is what makes a run
    entering scoring dispatch its own review without anyone issuing the
    ``reckon crew dispatch`` a coordinator would otherwise have to retype. A
    run whose review is already stored or already in flight is left alone, so
    the sweep is idempotent and the negative half of the property holds — a
    reflex that re-fires would manufacture runs rather than reviews.

    The same sweep carries the accepting branch: a promotable run whose stored
    review is clean and whose recorded gate still passes at the merged head is
    promoted here, with no coordinator command in between. A run that is not
    acceptance-eligible is left for its coordinator exactly as before.

    ``session`` names the sweeping session, and only runs whose pointer records
    that ownership: its lane and its member belong to the coordinator that
    chose them. Left unset it is resolved from the follower registration this
    process holds, so a sweep inside a follower is confined to its own session
    without the caller having to declare it.
    """
    reports: list[dict[str, Any]] = []
    dispatched: list[str] = []
    refused: list[dict[str, Any]] = []
    accepted: list[str] = []
    awaiting_lane: list[str] = []
    lane_paused: list[str] = []
    repaired: list[str] = []
    sweeping = session if session is not None else _sweeping_session(project)
    for pointer in list_live(project=project):
        if (
            str(pointer.get("project") or "")
            and project
            and str(pointer.get("project")) != project
        ):
            continue
        if sweeping and str(pointer.get("session") or "") != sweeping:
            # Another session's run: its review is that session's to compose,
            # because only that coordinator chose its lane and its member.
            continue
        # A review run is never its own source run: counting one as a run
        # awaiting review is what composes a review of a review, and each link
        # of that chain is a real dispatch against a real member.
        if _is_review_node(pointer):
            continue
        # A run resumed since its queued review was recorded carries a head the
        # review no longer speaks for; the queued attempt is withdrawn rather
        # than left standing against a revision the run has moved past.
        withdraw_superseded_review(pointer)
        scan: dict[str, Any] | None = None
        try:
            scan = classify_pointer(pointer)
        except Exception:  # noqa: BLE001 - one unreadable run must not stop the sweep
            scan = None
        if scan is not None and scan["classification"] == "scoring":
            # A run whose tier is none changes no runtime source, so promotion
            # lands it with the review gate standing down and records the tier
            # as the reason no review exists. Composing a review for it anyway
            # spends a member and a lane on a diff no reviewer is owed, so the
            # tier is resolved through promotion's own resolver and the skip is
            # recorded here rather than left as a silent absence.
            run_id = str(pointer.get("run_id") or "")
            remaining = review_settle_seconds_remaining(pointer)
            if remaining > 0:
                # A run resumed within the settle window is still moving toward
                # the head it will be reviewed at; composing now would read a
                # revision the run is about to leave, so the sweep waits.
                reason = (
                    "the run was resumed less than five minutes ago, so its "
                    "head has not settled; the reflex waits before reviewing it"
                )
                _record_review_dispatch(run_id, status="settling", reason=reason)
                reports.append(
                    {
                        "run_id": run_id,
                        "dispatched": False,
                        "settling_seconds": int(remaining),
                        "reason": reason,
                    }
                )
                continue
            tier = _sweep_review_tier(pointer, scan.get("manifest_commits") or [])
            if tier == review_tiers.NONE:
                reason = (
                    "the run changes no runtime source, so its review tier is "
                    "none; no review is dispatched and the merged-head gate "
                    "checks it instead"
                )
                _record_review_dispatch(run_id, status="skipped", reason=reason)
                reports.append(
                    {
                        "run_id": run_id,
                        "dispatched": False,
                        "review_tier": review_tiers.NONE,
                        "reason": reason,
                    }
                )
            else:
                report = dispatch_review_for_run(
                    pointer, config=config, launcher=launcher
                )
                reports.append(report)
                if report.get("dispatched"):
                    dispatched.append(str(report.get("review_run_id") or ""))
                elif report.get("awaiting_lane"):
                    awaiting_lane.append(str(report.get("run_id") or ""))
                elif report.get("error") == "lane-paused":
                    lane_paused.append(str(report.get("run_id") or ""))
                elif report.get("refused"):
                    # The report itself, not a generator over it: a refusal list
                    # is read and serialized by whoever consumes the sweep, and a
                    # generator is neither readable nor JSON-serializable, so the
                    # refusal would be lost at exactly the moment a reader needs
                    # to know which lane was refused.
                    refused.append(report)
        # The accepting branch: a promotable run whose stored review is clean is
        # promoted here, without a coordinator command, once its own recorded
        # gate has passed and the gate still passes at the repository's merged
        # head. A run that is not acceptance-eligible returns None and is left
        # to its coordinator, and an eligible run whose acceptance was refused
        # is reported with its reason rather than silently skipped.
        if scan is not None and scan["classification"] == "promotable":
            acceptance = accept_clean_review(pointer, config=config)
            if acceptance is not None:
                reports.append(acceptance)
                if acceptance.get("accepted"):
                    accepted.append(str(acceptance.get("run_id") or ""))
        # The repair pass, keyed on the stored review rather than on the run's
        # classification: a review carrying findings may leave the run reading
        # promotable, so gating this on ``scoring`` alone would leave a
        # finding-bearing round unrepaired. Its guards — role, identity,
        # liveness, record-only findings, and a pointer re-read at launch —
        # leave every run they do not apply to untouched, so sweeping the rest
        # of the fleet is unaffected.
        repair_report = dispatch_repair_for_run(
            pointer, config=config, launcher=launcher
        )
        if repair_report.get("dispatched"):
            reports.append(repair_report)
            repaired.append(str(repair_report.get("repair_run_id") or ""))
    return {
        "reports": reports,
        "dispatched": dispatched,
        "refused": refused,
        "accepted": accepted,
        "awaiting_lane": awaiting_lane,
        "lane_paused": lane_paused,
        "repaired": repaired,
    }


from .recovery_classification import (  # noqa: E402
    classify_pointer,
)
from .recovery_repair_dispatch import (  # noqa: E402
    _sweep_review_tier,
    _sweeping_session,
    dispatch_repair_for_run,
)
from .recovery_review_dispatch import (  # noqa: E402
    _record_review_dispatch,
    _review_head_and_tree,
    dispatch_review_for_run,
    review_settle_seconds_remaining,
    withdraw_superseded_review,
)
from .recovery_review_subject import (  # noqa: E402
    _is_review_node,
    _review_is_complete,
    select_review_for_head,
)
