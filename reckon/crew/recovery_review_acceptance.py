# ruff: noqa: I001, UP035
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from reckon import review_tiers
from reckon.crew import runs
from reckon.crew import review as review_module
from reckon.crew.node import (
    CrewError,
)
from reckon.crew.runs import (
    list_live,
    record_process_alive,
)


#: The reason a non-blocking finding is recorded under when the landing pass
#: lands the run it was raised against. The emissary is the landing pass itself,
#: so the reason names where the judgement was made rather than what the
#: reviewer thought.
LANDED_NON_BLOCKING_REASON = "non-blocking, noted at landing"


def _stored_findings(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A stored review's findings, enumerated as every other reader enumerates them.

    The repair reflex, the findings surface and the disposition writer all read
    a record through :func:`reckon.crew.repair.review_findings`, which skips an
    entry that states no claim because no reader can answer it back. This gate
    and the exemption writer beside it use that same enumeration, so an index
    the landing pass exempts is the index the writer keys the disposition
    against rather than one counted over entries the readers drop.
    """
    from reckon.crew import repair as repair_module

    return repair_module.review_findings(review)


def _finding_blocks(finding: Mapping[str, Any]) -> bool:
    """Whether one stored finding withholds the run's landing.

    Only a declared non-blocking severity is excused. A finding that declares
    nothing — no key, an empty value, a word outside the vocabulary — is a
    judgement the reviewer left unstated, and the repair reflex treats that as
    blocking; a gate that read it as non-blocking would let an unreadable
    severity slip a reviewer's finding past the landing.
    """
    severity = review_module.declared_severity(finding)
    return severity is None or severity == review_module.BLOCKING_FINDING_SEVERITY


def _blocking_finding_indexes(review: Mapping[str, Any] | None) -> tuple[int, ...]:
    """The indexes of the findings that withhold the run's landing."""
    if not isinstance(review, Mapping):
        return ()
    return tuple(
        index
        for index, finding in enumerate(_stored_findings(review))
        if _finding_blocks(finding)
    )


def _excusable_finding_indexes(review: Mapping[str, Any] | None) -> tuple[int, ...]:
    """The indexes of the findings nobody has answered and landing may excuse.

    A finding that already carries a disposition is its author's answer, written
    before the run reached here: a coordinator folds it into a follow-up node
    and names that node, or exempts it with a reason of their own. Recording an
    exemption over such an entry replaces the answer with this module's own and
    loses the node a fold names, so an answered finding is not excused again.
    """
    from reckon.crew import findings as findings_module

    if not isinstance(review, Mapping):
        return ()
    return tuple(
        index
        for index, finding in enumerate(_stored_findings(review))
        if not _finding_blocks(finding)
        and findings_module.finding_disposition(review, index) == "open"
    )


def _scores_are_in_range(review: Mapping[str, Any]) -> bool:
    """Whether every stored dimension score is a whole number the rubric allows.

    The parser refuses to write a score outside ``0..REVIEW_MAX_SCORE``, so a
    record carrying one was written by something other than the parser — an
    older writer, a hand edit, a repair that rewrote the file. The scores are
    what every later reader compares against, and a figure outside the schema
    would make those comparisons meaningless, so the landing rule treats such a
    record as unread rather than landing on it.
    """
    scores = review.get("scores")
    if not isinstance(scores, Mapping):
        return False
    for value in scores.values():
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if not 0 <= value <= review_module.REVIEW_MAX_SCORE:
            return False
    return True


def _record_is_complete(review: Mapping[str, Any] | None) -> bool:
    """Whether a stored review carries every measure the landing rule reads.

    Two questions, one answer: the record holds the measures the rubric asks
    for, and every score it holds is one the schema allows. Both are schema
    checks on the same record, so a reader asking whether a review may land asks
    them together — a record that fails either is one this rule cannot read.
    """
    return _review_is_complete(review) and _scores_are_in_range(review or {})


def _review_accepts_promotion(review: Mapping[str, Any] | None) -> bool:
    """Whether a stored review is clean enough to promote the run it read.

    Clean means complete — every dimension scored, every score inside the
    schema, and a total parsed — with no finding that withholds the landing. The
    score sets no bar: a reviewer who scored the work low has said so in the
    scores, and a run whose review raised nothing blocking is landed on that
    judgement rather than held from a coordinator who would read the same
    record. A review that states a non-blocking finding still accepts: the
    reviewer judged it not to hold the node, so it is recorded against the run
    as exempted at landing.
    """
    if not _record_is_complete(review):
        return False
    return not _blocking_finding_indexes(review)


def record_landed_exemptions(
    project: str,
    reviewed_run_id: str,
    review: Mapping[str, Any],
    *,
    reviewed_head_sha: str | None = None,
    base_dir: str | Path | None = None,
) -> list[int]:
    """Record the review's unanswered non-blocking findings as exempted.

    A run lands because its review raised nothing blocking, so a finding nobody
    answered must not vanish with the pointer that carried it: it is written
    into the stored record's own dispositions, naming the landing pass and the
    reason, and returned. A finding that already carries a disposition is
    skipped — it is the answer its author gave, and this writer would replace a
    fold's node with a reason of its own. The record rewritten is the one for
    the head this review read, so an exemption recorded against one revision
    never speaks for another.
    """
    from reckon.crew import review_dispositions

    # The dispositions are read from the record this writer rewrites, not from
    # the mapping the caller holds: a caller that parsed a review in memory
    # carries no dispositions, and reading them there would report every
    # finding unanswered and overwrite the answers the store already holds.
    stored = review_dispositions.stored_record_for_disposition(
        project,
        reviewed_run_id,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
    )
    answered = stored if isinstance(stored, Mapping) else review
    recorded: list[int] = []
    for index in _excusable_finding_indexes(answered):
        review_dispositions.record_finding_disposition(
            project,
            reviewed_run_id,
            index,
            kind="exempted",
            reason=LANDED_NON_BLOCKING_REASON,
            reviewed_head_sha=reviewed_head_sha,
            base_dir=base_dir,
        )
        recorded.append(index)
    return recorded


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


#: The checkout has no branch checked out, so the branch a remote comparison
#: would be made against is unknown. It is not an unreachable remote: the
#: repair is to check the primary branch out, and retrying changes nothing.
DETACHED_HEAD = "detached-head"

#: The run names a commit this checkout cannot resolve, so its ancestry cannot
#: be asked at all. Not an unreachable remote either: the repair is to fetch the
#: commit or correct the citation, not to retry the network.
UNRESOLVABLE_COMMIT = "unresolvable-commit"


def _checked_out_branch(repository: Path) -> tuple[str, str]:
    """The branch the checkout carries, or an empty name and the reason it has none."""
    probe = subprocess.run(
        ["git", "-C", str(repository), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.stdout.strip(), (probe.stderr or probe.stdout).strip()


def _names_a_commit(repository: Path, revision: str) -> bool:
    """Whether ``revision`` resolves to a commit in the checkout's object store."""
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{revision}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def _commits_missing_from(
    repository: Path, commits: Sequence[str]
) -> tuple[list[str], dict[str, Any] | None]:
    """The named commits the remote primary branch does not carry, and any refusal.

    A run's work is only landable once a coordinator has merged it onto the
    branch the remote carries. A checkout whose ``main`` was fast-forwarded and
    whose push then failed carries the run's commits locally while the remote
    has never seen them, so reading the checkout's own HEAD would promote work
    no other reader can find. The answer therefore comes from the module that
    lands work, :func:`reckon.crew.merge.head_on_remote_branch`, and is the
    same ancestry the merge lands by rather than a second convention.

    The remote is the authority, so an answer that is not "on the branch" is
    never read as "not missing". A remote that cannot be read, one carrying no
    primary ref, and a checkout configuring no remote at all each return a
    refusal naming the cause the coordinator must repair, because each would
    otherwise promote work no other reader can find. A detached HEAD and a
    commit this checkout cannot resolve are told apart from the remote causes
    for the same reason: they have different repairs, and reporting either as
    an unreachable remote sends the coordinator to the network for a checkout
    that needs a branch or a fetch.
    """
    from reckon.crew import merge as merge_module

    shas = [str(commit).strip() for commit in commits if str(commit).strip()]
    if not shas:
        return [], None
    branch, branch_detail = _checked_out_branch(repository)
    if not branch:
        return [], {"cause": DETACHED_HEAD, "detail": branch_detail}
    missing: list[str] = []
    for sha in shas:
        if not _names_a_commit(repository, sha):
            return [], {"cause": UNRESOLVABLE_COMMIT, "head": sha}
        try:
            answer = merge_module.head_on_remote_branch(repository, sha, branch=branch)
        except merge_module.MergeError as fault:
            return [], {
                "cause": merge_module.REMOTE_UNREACHABLE,
                "head": sha,
                "remote": "origin",
                "branch": branch,
                "detail": str(fault),
            }
        cause = answer.get("cause")
        if cause is not None:
            return [], {
                "cause": cause,
                "head": sha,
                "remote": answer.get("remote"),
                "branch": answer.get("branch") or branch,
                "detail": answer.get("detail"),
            }
        if not answer.get("on_branch"):
            missing.append(sha)
    return missing, None


def _remote_unreadable_reason(
    refusal: Mapping[str, Any], commits: Sequence[str]
) -> str:
    """Why acceptance could not ask the remote, in the words a coordinator reads.

    Each cause names the repair it needs rather than one blanket "could not
    read the remote": a detached checkout is fixed by checking out the primary
    branch, an unresolvable commit by fetching it or correcting the citation,
    an absent remote by configuring one, and only an unreachable remote is a
    network problem.
    """
    from reckon.crew import merge as merge_module

    names = ", ".join(
        str(commit).strip()[:12] for commit in commits if str(commit).strip()
    )
    cause = refusal.get("cause")
    remote = str(refusal.get("remote") or "origin")
    branch = str(refusal.get("branch") or "the primary branch")
    if cause == DETACHED_HEAD:
        return (
            "the checkout's HEAD is detached, so there is no primary branch to "
            f"compare the run's commits ({names}) against; check the primary "
            "branch out"
        )
    if cause == UNRESOLVABLE_COMMIT:
        return (
            f"the checkout cannot resolve commit {refusal.get('head')}, so the "
            f"run's commits ({names}) cannot be shown to be on {branch}; fetch "
            "the commit or correct the citation"
        )
    if cause == merge_module.REMOTE_UNCONFIGURED:
        return (
            f"the checkout configures no {remote} remote, so there is no "
            f"remote {branch} to show the run's commits ({names}) on; add the "
            "remote, or merge and push the run"
        )
    if cause == merge_module.REMOTE_UNREACHABLE:
        return (
            f"{remote} could not be read, so it is unknown whether the run's "
            f"commits ({names}) are on {branch}: "
            f"{str(refusal.get('detail') or '').strip()}"
        )
    return (
        f"{remote} carries no {branch}, so the run's commits ({names}) are not "
        "on the primary branch"
    )


def accept_clean_review(
    pointer: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Promote the reviewed run when its stored review is clean.

    When a review run completes with a stored record that is complete and
    carries no blocking finding, the reviewed run is promoted without a
    coordinator, whatever its scores: the non-blocking findings it
    does carry are recorded as deferred, by the writer the landing path uses, the
    commits its manifest names must already be ancestors of the repository's head,
    the run's own recorded gate
    must already have passed, the gate is re-run against the repository's
    current head with the run merged, and on all of those passing the run is
    promoted with ``promoted_by: review-acceptance`` on its ledger row.
    Anything short of that returns to the coordinator exactly as
    before, so this is the accepting branch beside the refusal that stops a
    promotion without a clean review rather than a bypass of it. Acceptance
    never merges: an unmerged run stands down and names the commits it found
    off the branch.

    None is returned when the run is not acceptance-eligible at all — no stored
    review, or a review that is incomplete or carrying a blocking finding —
    because those runs are the coordinator's and this handler has
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
    # The run's work must already be on the branch the remote carries. A run
    # reaching acceptance straight after its review has not been merged, so a
    # gate re-run here would verify a tree the change is not in and record a
    # promotion whose commits never landed. A remote that cannot be read, or
    # that carries no primary ref, is that same state with no way to check it,
    # so acceptance stands down naming the cause rather than promoting blind.
    missing, remote_refusal = _commits_missing_from(checkout, commits)
    if remote_refusal is not None:
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": _remote_unreadable_reason(remote_refusal, commits),
            "remote_cause": remote_refusal.get("cause"),
        }
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
    # Read only once the promotion has happened: a deferral written before it
    # would mark a finding deferred on a run the promotion then refused.
    #
    # The findings are recorded as deferred, by the writer the landing pass
    # uses, because a run promoted here and a run landed there are one run
    # arriving by two paths. An exemption written here would close a finding
    # that path leaves open work, so which path promoted a run would decide
    # whether its unreviewed findings were still owed.
    from reckon.crew import review_dispositions

    deferred: list[int] = []
    deferral_refusal = ""
    try:
        deferred = review_dispositions.record_landed_deferrals(
            project, run_id, reviewed_head_sha=head or None
        )
    except (OSError, ValueError) as refusal:
        deferral_refusal = (
            "the run was promoted but its review's non-blocking findings could "
            f"not be recorded as deferred: {refusal}"
        )
    return {
        "run_id": run_id,
        "accepted": True,
        "promoted": True,
        "review": {
            "total": review.get("total") if review else None,
            "blocking_findings": 0,
            "deferred_findings": len(deferred),
        },
        **({"deferral_refusal": deferral_refusal} if deferral_refusal else {}),
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


def promote_review_run_itself(
    pointer: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Promote a completed review run itself, once its record is stored.

    A review run's whole deliverable is the record it writes for the run it
    reads, so nothing a coordinator would add stands between that record
    landing and the run's own promotion. Once its manifest reads complete, its
    worker and supervisor have exited, and the record it was dispatched to
    store parses, the run is promoted with ``promoted_by: review-self`` on its
    ledger row and its live pointer is deleted, so the run no longer counts as
    unreconciled at dispatch. The reviewed run is untouched: acceptance of it
    stays scoped to merged runs, so this branch adds an act rather than
    widening that one.

    This runs for review-role pointers only, and it promotes the review run
    itself and never dispatches a review of it, so no chain of reviews of
    reviews can start. The reflex's skip of every review node stays in force
    for acceptance and for review dispatch. A review run whose manifest is not
    complete, whose worker is still alive, whose supervisor has not recorded
    an exit, or whose record is missing or unparsed is left for the
    coordinator, and the refusal returned names which of those held.

    ``None`` is returned for a run that is not a review run at all, because an
    ordinary or implement run is never promoted by this path. A dict is
    returned whenever a review run was considered, so the sweep records why an
    unpromoted review is still waiting.
    """
    if not _is_review_run(pointer):
        return None
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return None
    project = str(pointer.get("project") or "")
    manifest_path = Path(str(pointer.get("manifest_path") or ""))
    if _complete_manifest_write_time(manifest_path) is None:
        return {
            "run_id": run_id,
            "promoted": False,
            "reason": (
                "the review run's manifest does not read complete, so its "
                "verdict has not been written"
            ),
        }
    # The worker must have exited: a live worker owns the run, and promoting it
    # would delete the pointer out from under a process still producing the
    # record this branch reads.
    if record_process_alive(pointer) is True:
        return {
            "run_id": run_id,
            "promoted": False,
            "reason": "the review run's worker is still alive",
        }
    # The supervisor records its exit in the run directory before it ends, so
    # its exit record is the run's receipt that both processes have stopped.
    if _run_exit_record(pointer) is None:
        return {
            "run_id": run_id,
            "promoted": False,
            "reason": (
                "the review run's supervisor has recorded no exit, so the run "
                "has not ended"
            ),
        }
    # A run review's delivery is identified by the head it was dispatched to
    # read, which its dispatch spells as the head-keyed record path it grants.
    # One declaring no such path cannot be told apart from a round that read an
    # earlier revision, so it is refused here naming the unnamed head rather
    # than promoted on whichever record happens to sit where it was told to
    # write.
    declares = _declared_head_keyed_record(pointer, project)
    if _is_run_review_node(pointer) and declares is None:
        return {
            "run_id": run_id,
            "promoted": False,
            "reason": (
                "the review run declares no head-keyed record path, so the "
                "head it was dispatched to read is unnamed; its record cannot "
                "be told from an earlier round's"
            ),
        }
    stored = _delivered_review_record(pointer, project)
    if stored is None:
        named = f" for head {declares[1]}" if declares is not None else ""
        return {
            "run_id": run_id,
            "promoted": False,
            "reason": (
                f"the review run stored no readable record{named} for the run "
                "it read, so it has nothing a coordinator would not add"
            ),
        }
    _, record_path = stored
    from reckon.crew import promotion as promotion_module

    checkout_value = str(pointer.get("repo") or "").strip()
    checkout = Path(checkout_value).expanduser() if checkout_value else None
    root = checkout if (checkout is not None and checkout.is_dir()) else None
    try:
        result = promotion_module.complete(
            run_id,
            gate="not-run",
            root=root,
            promoted_by="review-self",
        )
    except (CrewError, OSError) as refusal:
        if isinstance(refusal, CrewError) and not runs.pointer_path(run_id).exists():
            raise
        return {
            "run_id": run_id,
            "promoted": False,
            "reason": str(refusal),
            "record_path": str(record_path),
        }
    return {
        "run_id": run_id,
        "promoted": True,
        "promoted_by": "review-self",
        "record_path": str(record_path),
        "ledger": {
            "path": str(result.get("ledger_path") or ""),
            "promoted_by": (
                str((result.get("record") or {}).get("promoted_by") or "")
                if isinstance(result.get("record"), Mapping)
                else "review-self"
            ),
        },
    }


def dispatch_awaiting_reviews(
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
    session: str | None = None,
    pointers: Sequence[Mapping[str, Any]] | None = None,
    promotions: Any = None,
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

    ``promotions`` hands each promotion and acceptance to a worker of its own
    instead of running it in this thread. A promotion takes minutes, so a sweep
    that ran them one after another held every review it owed behind them. The
    reviews are dispatched in the pass over the runs; the promotions are handed
    over only once that pass is done, so no promotion can complete before the
    reviews this sweep owed are dispatched. ``None`` keeps the older behaviour
    of running each promotion at the point the run is read, which is what a
    caller that wants one run's outcome in the returned report asks for.
    """
    reports: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    vanished: list[dict[str, Any]] = []
    dispatched: list[str] = []
    refused: list[dict[str, Any]] = []
    accepted: list[str] = []
    self_promoted: list[str] = []
    review_self_refused: list[dict[str, Any]] = []
    awaiting_lane: list[str] = []
    lane_paused: list[str] = []
    repaired: list[str] = []
    in_flight: list[dict[str, Any]] = []
    handed: list[tuple[str, str, Callable[[], dict[str, Any] | None]]] = []

    def _run_job(
        run_id: str, job: Callable[[], dict[str, Any] | None]
    ) -> dict[str, Any]:
        """Run one promotion or acceptance and name its outcome.

        The exception is classified where it is raised, because a pointer that
        has gone since it was read tells a vanished run from one that failed.
        """
        try:
            return {"run_id": run_id, "result": job()}
        except Exception as exc:  # noqa: BLE001 - each run's turn is its own
            entry = {
                "run_id": run_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
            if isinstance(exc, CrewError) and not runs.pointer_path(run_id).exists():
                return {"run_id": run_id, "vanished": entry}
            return {"run_id": run_id, "failed": entry}

    def _apply(kind: str, run_id: str, outcome: Mapping[str, Any]) -> None:
        if "vanished" in outcome:
            vanished.append(outcome["vanished"])
            return
        if "failed" in outcome:
            failed.append(outcome["failed"])
            return
        if outcome.get("in_flight"):
            in_flight.append(
                {
                    "run_id": run_id,
                    "kind": kind,
                    "reason": str(outcome.get("reason") or ""),
                }
            )
            return
        result = outcome.get("result")
        if result is None:
            return
        if kind == "review-self":
            if result.get("promoted"):
                self_promoted.append(str(result.get("run_id") or ""))
            else:
                # A refused self-promotion is kept in its own list rather than
                # in ``reports``: that list is what a caller reads to find the
                # runs a review was composed or accepted for, and a review run
                # only ever promotes itself, so it is not a run awaiting review
                # and must not displace that reading.
                review_self_refused.append(result)
            return
        reports.append(result)
        if result.get("accepted"):
            accepted.append(str(result.get("run_id") or ""))

    def _hand_off(
        run_id: str, kind: str, job: Callable[[], dict[str, Any] | None]
    ) -> None:
        if promotions is None:
            _apply(kind, run_id, _run_job(run_id, job))
            return
        handed.append((run_id, kind, job))

    sweeping = session if session is not None else _sweeping_session(project)
    live = list_live(project=project) if pointers is None else pointers
    for pointer in runs._tick_pointers(live):
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
        try:
            # Every review run is handled here and not by the compose and accept
            # passes below: a review run is never its own source run, because
            # counting one as a run awaiting review is what composes a review of a
            # review, and each link of that chain is a real dispatch against a real
            # member. A review run whose stored record is present promotes itself;
            # one that is not ready is left for its coordinator and is never
            # dispatched a review of.
            if _is_review_run(pointer):
                # Promotion can reconstruct a missing pointer from durable records;
                # a sweep acts only on the live run it was asked to handle.
                run_id = str(pointer.get("run_id") or "")
                runs.read_pointer(run_id)
                _hand_off(
                    run_id,
                    "review-self",
                    lambda pointer=pointer: promote_review_run_itself(
                        pointer, config=config
                    ),
                )
                continue
            # A run resumed since its queued review was recorded carries a head the
            # review no longer speaks for; the queued attempt is withdrawn rather
            # than left standing against a revision the run has moved past.
            withdraw_superseded_review(pointer)
            scan = classify_pointer(
                pointer, **({"pointers": live} if pointers is not None else {})
            )
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
                _hand_off(
                    str(pointer.get("run_id") or ""),
                    "acceptance",
                    lambda pointer=pointer: accept_clean_review(pointer, config=config),
                )
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
        except Exception as exc:  # noqa: BLE001 - isolate each run's whole turn
            run_id = str(pointer.get("run_id") or "")
            entry = {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}
            if isinstance(exc, CrewError) and not runs.pointer_path(run_id).exists():
                vanished.append(entry)
            else:
                failed.append(entry)
    # The reviews this sweep owed are dispatched by now, so the promotions go
    # to their workers with nothing left waiting behind them. A run another
    # sweep is already promoting is reported in flight rather than promoted a
    # second time beside it.
    if promotions is not None:
        for run_id, kind, job in handed:

            def _job(
                job: Callable[[], dict[str, Any] | None] = job,
                run_id: str = run_id,
                kind: str = kind,
            ) -> dict[str, Any]:
                return {**_run_job(run_id, job), "kind": kind}

            if not promotions.submit(run_id, _job):
                in_flight.append(
                    {
                        "run_id": run_id,
                        "kind": kind,
                        "reason": (
                            "another sweep is already promoting this run, so "
                            "this one leaves it to that promotion"
                        ),
                    }
                )
        for outcome in promotions.wait():
            _apply(
                str(outcome.get("kind") or ""),
                str(outcome.get("run_id") or ""),
                outcome,
            )
    return {
        "reports": reports,
        "failed": failed,
        "vanished": vanished,
        "dispatched": dispatched,
        "refused": refused,
        "accepted": accepted,
        "self_promoted": self_promoted,
        "in_flight": in_flight,
        "review_self_refused": review_self_refused,
        "awaiting_lane": awaiting_lane,
        "lane_paused": lane_paused,
        "repaired": repaired,
    }


from .recovery_classification import (  # noqa: E402
    classify_pointer,
)
from .recovery_liveness import (  # noqa: E402
    _run_exit_record,
)
from .recovery_review_delivery import (  # noqa: E402
    _complete_manifest_write_time,
    _declared_head_keyed_record,
    _delivered_review_record,
    _is_review_run,
    _is_run_review_node,
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
    _review_is_complete,
    select_review_for_head,
)
