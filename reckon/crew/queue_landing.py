"""Land delivered, accepted implementation runs from the admission service."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from reckon import checkout_trust, ledger
from reckon.crew import landing_measure, recovery, runs
from reckon.crew.node import CrewError, is_collectible_test_path
from reckon.crew.promotion_records import _node_test_files
from reckon.crew.recovery_review_acceptance import _review_accepts_promotion
from reckon.crew.resume import hold_reason as delivery_hold_reason

_run_srun = subprocess.run


class _LandedElsewhereError(Exception):
    """The selected run no longer needs a landing attempt here."""


def _landed_elsewhere(pointer: Mapping[str, Any], run_id: str) -> bool:
    """Read current ownership, not the live-set snapshot used for selection."""
    try:
        runs.read_pointer(run_id)
    except CrewError:
        if not runs.pointer_path(run_id).is_file():
            return True
        raise
    project = str(pointer.get("project") or "")
    repo = str(pointer.get("repo") or "")
    row_path = ledger.run_path(project, run_id, ledger._run_ledger_root(project, repo))
    if not row_path.is_file():
        return False
    try:
        row = json.loads(row_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CrewError(f"ledger row unreadable for {run_id!r}: {exc}") from exc
    if not isinstance(row, Mapping) or row.get("run_id") != run_id:
        raise CrewError(f"ledger row names a different run: {row_path}")
    return True


# A revision as a manifest entry may spell it: hexadecimal, at least the
# seven characters git abbreviates to.
_REVISION = re.compile(r"[0-9A-Fa-f]{7,40}")

#: Seconds before a refusal that is environmental — a measurement bound that
#: was exceeded, or a base checkout that could not be built — is judged again.
#: Neither says anything about the run's code, so the run must be re-read even
#: though its code and review have not moved.
RETRY_BACKOFF_SECONDS = 900.0

#: The cap on that back-off. It doubles per repeated timeout, and without a cap
#: a run stuck on a permanently slow host would drift out of reach.
RETRY_BACKOFF_CAP_SECONDS = 6 * 60 * 60.0


def _delivered_revision(entries: Sequence[Any]) -> str:
    """The revision the last ``manifest_commits`` entry names, or "".

    A worker writes its commits however its manifest reads to it: a bare
    revision, a revision with its subject after it, or several revisions on one
    line. An entry whose every token is a revision is a bare list and its last
    token is the delivered commit. Any other entry leads with the revision and
    carries a subject after it, so its first token is the commit — and only
    while that token really is a revision. A subject word that happens to be
    hexadecimal (``deadbeef``), or an entry that leads with prose, therefore
    never stands in for a commit: the answer is "" so no head is guessed, and
    the caller reports the entry it could not read.
    """
    if not entries:
        return ""
    tokens = str(entries[-1] or "").split()
    if not tokens:
        return ""
    if all(_REVISION.fullmatch(token) for token in tokens):
        return tokens[-1]
    first = tokens[0]
    return first if _REVISION.fullmatch(first) else ""


def _unreadable_entry_guard(entries: Sequence[Any]) -> str:
    """A refusal naming the manifest commits entry no revision could be read
    from, or "" when there is no entry to name.

    An entry the parser cannot read is the run's own defect, not an absence of
    work: it is reported on the live pointer with the entry quoted, so a
    coordinator can correct the manifest instead of the run disappearing from
    the pass with nothing written down.
    """
    if not entries:
        return ""
    entry = str(entries[-1] or "").strip()
    if not entry:
        return ""
    return (
        "delivered revision: the manifest commits entry "
        f"{entry!r} names no revision at its head"
    )


def _unreadable_entry_holds(
    previous: Mapping[str, Any] | None, head: str, guard: str, code_stamp: str
) -> bool:
    """Whether this head already carries this refusal under this code."""

    if not isinstance(previous, Mapping):
        return False
    return (
        previous.get("status") == "refused"
        and recovery.same_revision(str(previous.get("head") or ""), head)
        and str(previous.get("guard") or "") == guard
        and bool(code_stamp)
        and str(previous.get("code_stamp") or "") == code_stamp
    )


#: The two review tiers a delivered run can owe. The code review is the run
#: review every landing reads; the figure review judges the rendered artifacts a
#: run that changed a figure produces, and is filed under its own rubric.
CODE_REVIEW_TIER = "code"
FIGURE_REVIEW_TIER = "figure"


def _owed_review_tiers(pointer: Mapping[str, Any]) -> list[str]:
    """Every review tier this run owes at its head, in the order it is read.

    The tier rule is the review reflex's own — the one it dispatches by — so the
    lander and the reflex cannot disagree about which reviews a run owes. A
    delivered run always owes its code review, and owes a figure review exactly
    when its commits add or change a figure image. A changed-path list git
    cannot produce reads as no figure review owed, which is the reflex's own
    reading of it: a lander waiting on a review the reflex would never dispatch
    would hold the run forever, so the two must answer alike.
    """
    from reckon.crew.recovery_review_dispatch import _changed_figure_paths

    tiers = [CODE_REVIEW_TIER]
    try:
        images = _changed_figure_paths(pointer)
    except (OSError, ValueError):
        images = []
    if images:
        tiers.append(FIGURE_REVIEW_TIER)
    return tiers


def _review_for_tier(
    project: str, run_id: str, head: str, tree: Path | None, tier: str
) -> tuple[dict[str, Any] | None, str]:
    """The stored review one tier owes at ``head``, and any other head seen.

    The code tier is read through the selection the classifier and promotion
    share, so the lander, the classifier and the gate all treat one record as
    the evidence about a revision. The figure tier is read through the helper
    promotion's own figure guard uses: the text-review selection excludes figure
    records by design, so reading it through that selection answers with the
    wrong subject, and reading it through any other reader lets the lander judge
    a record promotion never consults.
    """
    from reckon.crew import promotion_checks
    from reckon.crew.recovery_review_subject import select_review_for_head

    if tier != FIGURE_REVIEW_TIER:
        return select_review_for_head(project, run_id, head, tree=tree)
    # The guard that refuses a promotion for a figure finding reads the newest
    # figure record through this owner, so the lander reads the same record:
    # judging an earlier one here would let the lander wait on, or land on,
    # evidence promotion never consults. The owner answers ``None`` for both a
    # run carrying no figure review and one whose record cannot be parsed, so
    # the absence is resolved here, through the owner's own account of which
    # files it could not read: a record nobody could parse may hold an
    # undisposed blocking finding, and taking it for no record at all is a
    # fail-open.
    record = promotion_checks._newest_figure_review(project, run_id)
    if record is not None:
        return record, ""
    unreadable = promotion_checks._unreadable_figure_review_records(project, run_id)
    if unreadable:
        raise _UnreadableFigureReviewError(unreadable)
    return None, ""


class _UnreadableFigureReviewError(Exception):
    """A figure review exists for the run and could not be parsed.

    It carries the owner's own account of the files it met and could not read,
    so the awaiting record names the record rather than reporting an absence.
    """

    def __init__(self, records: Sequence[Mapping[str, str]]) -> None:
        self.records = [dict(entry) for entry in records]
        super().__init__(
            "; ".join(f"{entry.get('path')}: {entry.get('error')}" for entry in records)
        )


def _undisposed_figure_findings(review: Mapping[str, Any] | None) -> str:
    """Render the blocking findings a stored figure review leaves open.

    The severity and the disposition are read through the one owner that
    decides both, which is the list promotion's figure guard refuses on, so the
    lander holds a run for exactly the findings the gate would refuse it for.
    """
    from reckon.crew import promotion_checks
    from reckon.crew import repair as repair_module

    if not isinstance(review, Mapping):
        return ""
    blocking = repair_module.blocking_findings(review)
    if not blocking:
        return ""
    return promotion_checks._describe_blocking_findings(blocking)


def _review_tier_is_complete(tier: str, review: Mapping[str, Any] | None) -> bool:
    """Whether a stored review of this tier holds every measure its rubric asks.

    The code tier is judged by the acceptance rule the landing already applies,
    so an incomplete or finding-bearing text review is not re-judged here. A
    figure review is not scored on the text dimensions. It is judged by the
    store's delivery contract for its rubric — a record met mid-write is still
    incomplete — and by whether it leaves a blocking finding undisposed, which
    is the predicate promotion's own figure guard uses. A figure review that
    passes the delivery contract but still holds an open blocking finding is
    not complete: it names a defect the figure fixes, so landing on it would
    carry that defect to the primary branch.
    """
    from reckon.crew import repair as repair_module
    from reckon.crew import review as review_module

    if tier != FIGURE_REVIEW_TIER:
        return _review_accepts_promotion(review)
    record = review or {}
    return not review_module.review_record_refusals(record) and not (
        repair_module.blocking_findings(record)
    )


def _owed_review_stamp(
    reviews: Mapping[str, Mapping[str, Any] | None],
) -> str:
    """The identity of every owed review record a judgement was made on.

    A refusal states the record it read, and holds while that record is
    unchanged. Reading only the code review would leave a run refused for a
    figure review it owed held after that review was stored: the stamp would
    not move and the refusal would stand over an absence that no longer
    exists. Keying the stamp on each owed tier's record makes storing the
    missing review a change the next pass reads as a reason to judge again.
    """
    from reckon.crew.plan_review import _digest

    return _digest(
        {
            str(tier): (_review_stamp(review) if review is not None else "")
            for tier, review in reviews.items()
        }
    )


def _review_reflex_state(
    pointer: Mapping[str, Any], tier: str = CODE_REVIEW_TIER
) -> tuple[str, str]:
    """The review reflex's own state for one tier, as ``(state, detail)``.

    A run waiting for its review is not a run with nothing to say: the reflex
    either dispatched a review or was refused, and it writes which onto the run
    it acted for. Reading that record beside the waiting run is what turns a
    silent skip into a verdict a coordinator can act on. A run carrying no
    record at all has not been picked up yet, which is reported as a refusal to
    dispatch with that reason rather than as an absence of state.

    Each tier has its own dispatch field, so a run waiting on its figure review
    reports the figure reflex's attempt rather than the code review's, which
    would name a dispatch that has already produced the review being read.
    """
    from reckon.crew.recovery_review_dispatch import (
        FIGURE_REVIEW_DISPATCH_FIELD,
        REVIEW_DISPATCH_FIELD,
    )

    field = (
        FIGURE_REVIEW_DISPATCH_FIELD
        if tier == FIGURE_REVIEW_TIER
        else REVIEW_DISPATCH_FIELD
    )
    recorded = pointer.get(field)
    if not isinstance(recorded, Mapping):
        return "refused", "the review reflex has recorded no attempt for this run"
    if str(recorded.get("status") or "") == "dispatched":
        return "dispatched", ""
    reason = str(recorded.get("reason") or "").strip()
    return "refused", reason or str(recorded.get("status") or "")


def _awaiting_review_holds(
    previous: Mapping[str, Any] | None,
    head: str,
    state: str,
    detail: str,
    code_stamp: str,
    tier: str = CODE_REVIEW_TIER,
    *,
    stale_head: str = "",
    read_error: str = "",
    findings: str = "",
    unreadable: Sequence[Mapping[str, str]] = (),
) -> bool:
    """Whether this head already carries this awaiting-review record."""

    if not isinstance(previous, Mapping):
        return False
    return (
        previous.get("status") == "awaiting-review"
        and recovery.same_revision(str(previous.get("head") or ""), head)
        and str(previous.get("review_state") or "") == state
        and str(previous.get("review_detail") or "") == detail
        and str(previous.get("awaiting_tier") or CODE_REVIEW_TIER) == tier
        and str(previous.get("stale_head") or "") == stale_head
        and str(previous.get("review_read_error") or "") == read_error
        and str(previous.get("review_findings") or "") == findings
        and list(previous.get("unreadable_reviews") or ()) == list(unreadable)
        and bool(code_stamp)
        and str(previous.get("code_stamp") or "") == code_stamp
    )


def _record_awaiting_review(
    run_id: str,
    pointer: Mapping[str, Any],
    *,
    head: str,
    code_stamp: str,
    review_stamp: str,
    tier: str = CODE_REVIEW_TIER,
    stale_head: str = "",
    read_error: str = "",
    findings: str = "",
    unreadable: Sequence[Mapping[str, str]] = (),
) -> None:
    """Record that this head's landing waits on one review tier.

    The run is not refused and not landed: it has a verdict, and the verdict
    names the tier it waits for and that tier's reflex state. Writing it once
    per head, tier and reflex state keeps a pass that runs every 30 seconds
    from rewriting the pointer for a run whose situation has not moved.

    The tier is filed under ``awaiting_tier``, never ``review_tier``: that key
    on the run record and the ledger holds the light, full or none sweep tier,
    so one name would hold two vocabularies and a reader of either would have
    to guess which it read.

    ``stale_head`` names a revision the tier's stored record describes instead
    of this one, so the record reports two revisions that disagree rather than
    an absence — it is a revision and never an error message, so a store read
    that raised is recorded in ``review_read_error`` instead. ``findings``
    names the undisposed blocking findings an existing review holds, and
    ``unreadable`` names the figure-record files that could not be parsed: each
    is the fact that explains the wait, kept apart from the others.
    """
    state, detail = _review_reflex_state(pointer, tier)
    if _awaiting_review_holds(
        pointer.get("admission_landing"),
        head,
        state,
        detail,
        code_stamp,
        tier,
        stale_head=stale_head,
        read_error=read_error,
        findings=findings,
        unreadable=unreadable,
    ):
        return

    def mutation(record: dict[str, Any]) -> dict[str, Any]:
        record["admission_landing"] = {
            "head": head,
            "status": "awaiting-review",
            "awaiting_tier": tier,
            "review_state": state,
            "review_detail": detail,
            "code_stamp": code_stamp,
            "review_stamp": review_stamp,
        }
        if stale_head:
            record["admission_landing"]["stale_head"] = stale_head
        if read_error:
            record["admission_landing"]["review_read_error"] = read_error
        if findings:
            record["admission_landing"]["review_findings"] = findings
        if unreadable:
            record["admission_landing"]["unreadable_reviews"] = [
                dict(entry) for entry in unreadable
            ]
        return record

    runs._mutate_pointer(run_id, mutation)


def _landing_code_stamp() -> str:
    """The content identity of the code that judges a landing.

    A refusal is a judgement made by a rule, so it holds only for the rule that
    made it: the stamp covers the package the landing pass runs from and moves
    when that code lands as a commit. An identity that cannot be read records
    nothing, so the next pass judges the refusal again rather than trusting a
    judgement it cannot place.
    """
    from reckon.crew.follower_registration import follower_code_stamp

    try:
        return follower_code_stamp()
    except (OSError, ValueError):
        return ""


def _review_stamp(review: Mapping[str, Any] | None) -> str:
    """The identity of the review record a judgement was made on, or "".

    Keyed on the record's content, so a reviewer who answers a finding, records
    a disposition or corrects a score rewrites the identity and the next pass
    reads the record as it now stands. A run with no readable record has no
    identity to keep, and an empty one never matches.
    """
    if not isinstance(review, Mapping):
        return ""
    from reckon.crew.plan_review import _digest

    return _digest(review)


#: The refusals that say the measurement could not be taken rather than that
#: the run is wrong. A timeout is a population that outgrew its bound on this
#: host, and a base-checkout failure is a scratch tree that could not be built;
#: neither is a finding about the delivered code, so neither holds without a
#: retry.
_ENVIRONMENTAL_GUARDS = (
    "landing measure unreadable: the base checkout could not be built",
)


def _is_environmental(guard: str) -> bool:
    """Whether a guard states an environmental failure rather than a finding.

    A refusal whose guard is a timeout or a base-checkout failure is judged
    again on a back-off; a refusal for an added regression, a blocking finding
    or a merge conflict stays hold until the code or the review moves.
    """
    text = str(guard or "")
    if text.startswith(_ENVIRONMENTAL_GUARDS):
        return True
    return (
        text.startswith("landing measure unreadable: ")
        and "exceeded its" in text
        and text.endswith("bound")
    )


#: The promotion guard a passing gate raises when the check it rests on is
#: missing a required field, and the word that introduces the field list.
_GATE_CHECK_GUARD = "a passing gate requires the check that produced it"

_GATE_CHECK_MISSING = re.compile(r"missing ([^\n;]+)")


def _manifest_evidence_remedy(pointer: Mapping[str, Any]) -> str:
    """The repair a gate refusal owes when promotion reads no manifest evidence.

    Promotion fills a passing gate's check through ``_fresh_manifest``, which
    ignores a manifest no newer than the attempt's
    ``manifest_baseline_mtime_ns`` — a run
    resumed onto a stale file has delivered nothing this attempt. No field can
    then be recovered from the manifest however well its cited log reads, so the
    remedy names the write the coordinator owes rather than a record to add to a
    log the promotion never opens.
    """
    manifest_path = str(pointer.get("manifest_path") or "").strip()
    present, fresh = runs._manifest_freshness(pointer)
    if present and fresh:
        return ""
    where = f"the manifest at {manifest_path}" if manifest_path else "no manifest"
    if not present:
        return (
            f"the promotion reads no gate evidence because this run has {where}: "
            "write the manifest for this attempt with its status, gate command "
            "and test_logs, so the landing reads the delivered evidence"
        )
    return (
        f"the promotion ignores {where} because it is not newer than this "
        "attempt began (manifest_baseline_mtime_ns): rewrite the manifest for "
        "this attempt — its status, gate command and test_logs — so the landing "
        "reads the delivered evidence, rather than editing the gate log alone"
    )


def _gate_log_cited_by_manifest(pointer: Mapping[str, Any]) -> str:
    """The gate log a run's own manifest cites, resolved beside the manifest.

    The selection is the promotion's own: ``_default_gate_evidence_from_manifest``
    is what fills a passing gate's check from a run's manifest, so a remedy
    naming a log names exactly the file a promotion would read — by the same
    freshness rule and the same first-entry choice — rather than a second copy
    of that rule drifting from it.
    """
    from reckon.crew.promotion_records import _default_gate_evidence_from_manifest

    try:
        check, _commits = _default_gate_evidence_from_manifest(
            pointer, verdict="passed", gate_check=None, commits=()
        )
    except (OSError, ValueError):
        return ""
    return str(check.get("log_path") or "").strip()


def _log_ends_in_exit_record(log_path: str) -> bool | None:
    """Whether a gate log's last line is a terminal ``EXIT=<n>`` record.

    ``None`` when the log cannot be read, because an absent file is not a log
    whose exit line is wrong, and the remedy must not assert a shape nobody
    read. The reader is the promotion's own, so a remedy never tells an author
    to write a line the gate would not accept.
    """
    from reckon.crew.promotion_evidence import _recorded_exit_status

    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _recorded_exit_status(text) is not None


def _gate_check_remedy(pointer: Mapping[str, Any], missing: Sequence[str]) -> str:
    """The one action that supplies each field a gate check is missing.

    The refusal's own words name only the absent field, and a coordinator who
    reads "missing exit_status" goes looking at the promotion command when the
    defect is usually the gate log's last line. Naming the log the manifest
    cites and the record it must end in turns that search into one edit.
    """
    stale = _manifest_evidence_remedy(pointer)
    if stale:
        return stale
    if "exit_status" in missing:
        log_path = _gate_log_cited_by_manifest(pointer)
        if log_path and _log_ends_in_exit_record(log_path) is False:
            return (
                f"the gate log {log_path} does not end in a terminal EXIT=<n> "
                "record, so the promotion has no exit status to read: re-run the "
                "gate with its output captured and `echo EXIT=$?` written last, "
                f"so {log_path} ends in a line reading EXIT=<n>"
            )
        return (
            "the promotion's gate check carries no exit_status: make the gate "
            "log the manifest cites end in a terminal EXIT=<n> record, or pass "
            "--gate-exit-status <n>"
        )
    if "command" in missing:
        return (
            "the promotion's gate check names no command: write the command a "
            "shell was given on the manifest's `tests:` line, or pass "
            "--gate-command '<command>'"
        )
    if "log path or digest" in missing:
        return (
            "the promotion cites no gate log: pass --gate-log-path <path> "
            "naming the file the gate's output was captured to"
        )
    return ""


def _landing_remedy(pointer: Mapping[str, Any], guard: str) -> str:
    """The action a landing refusal's guard calls for, or "" when it names none.

    Only a guard this module can state a remedy for gets one: a guard it does
    not understand is left exactly as recorded rather than given a remedy that
    may be wrong.
    """
    if _GATE_CHECK_GUARD not in str(guard or ""):
        return ""
    match = _GATE_CHECK_MISSING.search(str(guard))
    if match is None:
        return ""
    missing = [part.strip() for part in match.group(1).split(",") if part.strip()]
    return _gate_check_remedy(pointer, missing)


def _retry_backoff(attempts: int) -> float:
    """Seconds to wait before judging an environmental refusal again.

    The wait doubles per repeated timeout and is capped, so a run whose host
    was briefly slow is retried soon while one that keeps timing out is retried
    no more often than the cap allows.
    """
    count = max(1, int(attempts or 1))
    try:
        waited = RETRY_BACKOFF_SECONDS * (2 ** (count - 1))
    except OverflowError:
        return RETRY_BACKOFF_CAP_SECONDS
    return min(waited, RETRY_BACKOFF_CAP_SECONDS)


def _retry_due(previous: Mapping[str, Any]) -> float | None:
    """When an environmental refusal recorded on the run may be judged again.

    ``None`` when the record carries no failure time, which is what a refusal
    stored before the retry clock was kept leaves: it says nothing about when
    it was made, so the run is judged once more rather than held on a guess.
    """
    failed_at = previous.get("failed_at")
    if isinstance(failed_at, bool) or not isinstance(failed_at, (int, float)):
        return None
    attempts = previous.get("attempts")
    count = (
        1 if isinstance(attempts, bool) or not isinstance(attempts, int) else attempts
    )
    return float(failed_at) + _retry_backoff(count)


def _refusal_holds(
    previous: Mapping[str, Any],
    code_stamp: str = "",
    review_stamp: str = "",
    now: float | None = None,
) -> bool:
    """Whether a recorded refusal still stands under this code and record.

    A refusal states the rule that made it and the record it read, and holds
    while both are unchanged. The one exception is a refusal whose guard is
    environmental: a timeout or a base-checkout failure is retried once its
    back-off has elapsed, with the code and the review unchanged, because
    waiting for either to move would hold a run behind a busy host. A refusal
    recorded before the stamps were kept carries none of them, so it says
    nothing about the current rule and the run is judged once more.
    """
    if not code_stamp or not review_stamp:
        return False
    if (
        str(previous.get("code_stamp") or "") != code_stamp
        or str(previous.get("review_stamp") or "") != review_stamp
    ):
        return False
    if not _is_environmental(str(previous.get("guard") or "")):
        return True
    due = _retry_due(previous)
    if due is None:
        return False
    return (time.time() if now is None else float(now)) < due


def _refusal_attempts(
    previous: Mapping[str, Any] | None, *, head: str, guard: str
) -> int:
    """How many times this head has been refused for the same kind of failure.

    Only an environmental refusal counts a repeat: a further timeout on the
    same head at the same kind of failure doubles its back-off, while a refusal
    for a finding starts the count again because it is a different judgement.
    """
    if not isinstance(previous, Mapping) or previous.get("status") != "refused":
        return 1
    if not recovery.same_revision(str(previous.get("head") or ""), head):
        return 1
    earlier = str(previous.get("guard") or "")
    if not (_is_environmental(earlier) and _is_environmental(guard)):
        return 1
    recorded = previous.get("attempts")
    count = (
        1 if isinstance(recorded, bool) or not isinstance(recorded, int) else recorded
    )
    return max(1, count) + 1


def _record_attempt(
    run_id: str,
    *,
    head: str,
    guard: str,
    status: str,
    code_stamp: str = "",
    review_stamp: str = "",
    previous: Mapping[str, Any] | None = None,
    remedy: str = "",
    now: float | None = None,
) -> None:
    """Keep a refusal on the live run, with what made it, so a later pass can
    tell a refusal that still stands from one the current rule would reverse.

    The record also keeps when the refusal was made and how many times this
    head has been refused for an environmental failure, because that pair is
    what a retry's back-off is counted from and how far it doubles.
    """

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["admission_landing"] = {
            "head": head,
            "guard": guard,
            "status": status,
            "code_stamp": code_stamp,
            "review_stamp": review_stamp,
            "attempts": _refusal_attempts(previous, head=head, guard=guard),
            "failed_at": time.time() if now is None else float(now),
        }
        if remedy:
            pointer["admission_landing"]["remedy"] = remedy
        return pointer

    try:
        runs._mutate_pointer(run_id, mutation)
    except CrewError:
        if runs.pointer_path(run_id).exists():
            raise
        runs._write_json(
            runs.run_dir(run_id) / "admission-landing.json",
            mutation({})["admission_landing"],
        )


def _record_suite_evidence(
    run_id: str, measured: landing_measure.Measurement, *, promoted: bool = False
) -> None:
    """Keep the measurement where a later reader can open it.

    The live pointer loses this at promotion, so the run's own directory holds
    the pair as well.
    """

    record = measured.as_dict()
    directory = runs.run_dir(run_id) / "landing-measure"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "measurement.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["suite_evidence"] = record
        return pointer

    if not promoted:
        runs._mutate_pointer(run_id, mutation)


def _measurement_path(run_id: str) -> Path:
    return runs.run_dir(run_id) / "landing-measure" / "measurement.json"


def _stored_measurement(
    run_id: str,
    head: str,
    command: str,
    primary: str = "",
    pointer: Mapping[str, Any] | None = None,
) -> landing_measure.Measurement | None:
    """The measurement already recorded for this head, or ``None``.

    A record is reusable for its delivered head, command and primary revision.
    A primary that has moved since is reusable when every commit between the
    two leaves the measured inputs unchanged, because such a move cannot change
    what the pair measured; any other move rebuilds the candidate and measures
    again. The candidate identity is required so the merge can consume exactly
    what was measured.

    A record the measurement could not be taken for is not one: it states the
    bound the arm exceeded or the failure to build the base tree, and reusing
    it would refuse the retry the back-off exists to allow. Such a head is
    measured again, under the bound its own timed-out arm has just widened.
    """
    if not command:
        return None
    try:
        record = json.loads(_measurement_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    if str(record.get("head") or "") != head:
        return None
    if str(record.get("command") or "") != command:
        return None
    if str(record.get("unreadable") or ""):
        return None
    if primary:
        population = record.get("population")
        if not isinstance(population, Mapping) or not population.get("candidate_sha"):
            return None
        measured_main = str(population.get("main_sha") or "")
        if measured_main != primary and not _moved_primary_keeps_the_measurement(
            _pointer_or_read(run_id, pointer), measured_main, primary
        ):
            return None
    return landing_measure.Measurement.from_dict(record)


def _pointer_or_read(
    run_id: str, pointer: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """The pointer a caller supplied, or the run's own when it supplied none."""
    if pointer is not None:
        return pointer
    try:
        return runs.read_pointer(run_id)
    except (OSError, ValueError, CrewError):
        return {}


def _measurement_record(run_id: str) -> dict[str, Any] | None:
    """One run's stored measurement, or ``None`` when it carries none."""
    try:
        record = json.loads(_measurement_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(record) if isinstance(record, Mapping) else None


def _pass_history(
    pointers: Sequence[Mapping[str, Any]], project: str
) -> list[dict[str, Any]]:
    """This project's stored measurements, read once for a whole landing pass.

    A bound is sized from how long this project's populations actually take, so
    the history is the measurement record of each of the project's own live
    runs — not a walk of the runs directory, which holds thousands of other
    projects' and promoted runs' entries and is on shared storage. The pass
    reads this once and hands the same list to every run it measures, so the
    number of reads is the project's run count rather than the directory's
    entry count.
    """
    records: list[dict[str, Any]] = []
    if not project:
        return records
    for pointer in pointers:
        if str(pointer.get("project") or "") != project:
            continue
        record = _measurement_record(str(pointer.get("run_id") or ""))
        if record is not None:
            records.append(record)
    return records


def _merge_conflicts(pointer: Mapping[str, Any], head: str) -> list[str]:
    """The paths on which the run's head conflicts with the primary branch.

    Read through ``git merge-tree``, which builds the merged tree without
    touching the checkout or moving the branch, so a conflicting run is refused
    before anything is written to the branch. An instrument that cannot answer —
    an unresolvable head, a checkout that is not a work tree — returns no
    conflict here and the landing step itself reports the failure, because
    inventing a conflict would refuse a run git never judged.
    """
    repo = Path(str(pointer.get("repo") or "").strip())
    if not head or not repo.is_dir():
        return []
    from reckon.crew import merge

    try:
        _merged_tree, conflicts = merge._merge_tree(
            repo, str(pointer.get("merge_primary_revision") or "HEAD"), head
        )
    except (merge.MergeError, OSError):
        return []
    return merge.refusing_conflicts(conflicts)


def _measure_config(pointer: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read measurement settings from the project checkout, before scratch trees."""
    from reckon import flight

    try:
        resolved = flight.resolve(
            str(pointer.get("project") or "") or None,
            checkout_path=str(pointer.get("repo") or "") or None,
        )
    except (OSError, ValueError, flight.FlightConfigError):
        return {}
    return resolved.config


def _measure_timeout(pointer: Mapping[str, Any]) -> float:
    """The wall-clock bound per measurement subprocess, from flight config."""
    return landing_measure.measure_timeout(_measure_config(pointer))


def _primary_revision(pointer: Mapping[str, Any]) -> str:
    """Pin the checkout revision the automatic landing would merge into."""
    from reckon.crew import merge

    try:
        return merge._rev_parse(Path(str(pointer.get("repo") or "")), "HEAD")
    except (OSError, merge.MergeError):
        return ""


#: The committed paths a measurement reads. A commit that leaves every one of
#: them unchanged cannot change what a delivered run was measured against, so
#: the measurement still stands and is re-applied to the primary it was taken
#: on rather than repeated. The catalogue is named separately from the package
#: because it lives under ``docs/`` yet is read by the running product, and the
#: dependency pins and the tests are named because a suite pair runs the
#: product's own tests against the product's own pins.
_MEASURED_INPUT_PATHS = (
    "reckon",
    "tests",
    "pyproject.toml",
    "uv.lock",
    checkout_trust.CATALOGUE_RELATIVE_PATH,
)


def _measured_product_tree(repo: Path, revision: str) -> tuple[str, ...] | None:
    """Git's own names for the measured inputs at ``revision``, or ``None``.

    One ``ls-tree`` answers for every path whether or not it exists, so a
    checkout that legitimately carries no catalogue reads as absent at both
    revisions rather than as a failure. A non-zero exit is git declining to
    answer, and ``None`` says so: an identity that cannot be read must not be
    mistaken for an unchanged one.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", revision, "--", *_MEASURED_INPUT_PATHS],
            capture_output=True,
            text=True,
            timeout=checkout_trust.IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    named: dict[str, str] = {}
    for line in result.stdout.splitlines():
        metadata, _, path = line.partition("\t")
        fields = metadata.split()
        if len(fields) >= 3:
            named[path] = fields[2]
    return tuple(named.get(path, "") for path in _MEASURED_INPUT_PATHS)


def _moved_primary_keeps_the_measurement(
    pointer: Mapping[str, Any], measured_main: str, primary: str
) -> bool:
    """Whether main moved only by commits that cannot change the measurement.

    A landing measures a delivery against the primary revision it merges onto,
    and every later commit used to discard that measurement, so a main moving
    with plan edits and review records defered every reviewed run indefinitely.
    A commit touching only files outside the measured inputs leaves the
    population, its tests and its pins exactly as measured, and the merged
    product the measurement judged is the one the landing will still produce.

    The package side is read through :func:`checkout_trust.committed_reckon_tree`,
    which names the content of ``reckon/`` at ``HEAD`` -- the primary being
    landed onto -- so a package commit is judged by the same content identity
    the fleet's reload readers use. Anything this cannot read is not judged
    unchanged: a measurement is reused only on a positive answer.
    """
    if not _REVISION.fullmatch(measured_main or "") or not _REVISION.fullmatch(
        primary or ""
    ):
        return False
    if measured_main == primary:
        return True
    repo = Path(str(pointer.get("repo") or ""))
    if not repo.is_dir():
        return False
    measured_tree = _measured_product_tree(repo, measured_main)
    primary_tree = _measured_product_tree(repo, primary)
    if measured_tree is None or primary_tree is None or measured_tree != primary_tree:
        return False
    current_package = checkout_trust.committed_reckon_tree(repo)
    return current_package is not None and current_package == measured_tree[0]


def _landing_test_files(
    pointer: Mapping[str, Any], changed: Sequence[str], tree: Path
) -> list[str]:
    """The test files the landing's suite pair runs.

    The node's own fenced test paths come first, as the armed command was
    resolved from them. A node whose write scope names no test file, which a
    batch entry taking its scope from section prose often is, still delivered
    the tests its done-when asked for, so the test files the delivery changed
    are measured instead of refusing the landing as having nothing to run. Only
    files pytest can collect and that are present in the measured tree are
    named, so a fixture is not handed to the runner and a deleted test is not
    passed to it.
    """
    named = _node_test_files(pointer)
    if named:
        return named
    return sorted(
        path
        for path in changed
        if is_collectible_test_path(path) and (Path(tree) / path).is_file()
    )


def _measure_delivery(
    pointer: Mapping[str, Any],
    run_id: str,
    head: str,
    tree: str,
    stored: Sequence[Mapping[str, Any]] = (),
    *,
    promoted: bool = False,
    request: Mapping[str, Any] | None = None,
) -> Any:
    """Measure current primary against the merge of one delivered run.

    A run that armed no command is unmeasurable and cannot land. A
    measurement that cannot be taken — no command, no base revision, or a base checkout
    that cannot be built — yields an unreadable measurement so the caller
    refuses the landing instead of inferring a verdict from nothing.

    The importer set follows the modules the run changed since dispatch, read
    in the candidate tree so it includes callers added on primary meanwhile.
    An already incorporated head is its own candidate; its base arm still runs
    at current primary, never at the dispatch revision.

    ``stored`` is the project's measurement history, read by the pass once for
    all its runs; a caller that names none measures with the floor as its only
    bound.
    """
    command = str(pointer.get("suite_command") or "").strip()
    if not command:
        measured = landing_measure.Measurement(
            command="",
            suite={},
            population={},
            unreadable="the run armed no suite command; configure one before landing",
            head=head,
            project=str(pointer.get("project") or ""),
        )
        _record_suite_evidence(run_id, measured, promoted=promoted)
        return measured
    base_sha = str(pointer.get("base_sha") or "").strip()
    project = str(pointer.get("project") or "")
    repo = Path(str(pointer.get("repo") or tree or ""))
    if not base_sha or not repo.is_dir():
        return landing_measure.Measurement(
            command=command,
            suite={},
            population={},
            unreadable="the run records no base revision to compare against",
            head=head,
            project=project,
        )
    config = request["config"] if request is not None else _measure_config(pointer)
    timeout = (
        landing_measure.measure_timeout(config)
        if request is not None
        else _measure_timeout(pointer)
    )
    logs = runs.run_dir(run_id) / "landing-measure"
    if request is not None:
        logs = Path(str(request["logs_dir"]))
    elif promoted:
        logs = logs / "promoted"
    primary = (
        str(request["primary"]) if request is not None else _primary_revision(pointer)
    )
    if not primary:
        measured = landing_measure.Measurement(
            command=command,
            suite={},
            population={},
            unreadable="the primary revision could not be read",
            head=head,
            project=project,
        )
        _record_suite_evidence(run_id, measured, promoted=promoted)
        return measured
    try:
        changed = landing_measure.delivered_paths(repo, base_sha, head)
        # Only the checkout is inside this except: an error raised by the
        # measurement itself is a different failure and is reported as one.
        with ExitStack() as stack:
            measured_head, measured_tree = head, Path(tree)
            if primary != head:
                if not promoted and _landed_elsewhere(pointer, run_id):
                    raise _LandedElsewhereError
                measured_head, measured_tree = stack.enter_context(
                    landing_measure.candidate_checkout(repo, primary, head, timeout)
                )
            elif request is not None:
                measured_tree = stack.enter_context(
                    landing_measure.base_checkout(repo, head, timeout)
                )
            if not promoted and _landed_elsewhere(pointer, run_id):
                raise _LandedElsewhereError
            base_tree = stack.enter_context(
                landing_measure.base_checkout(repo, primary, timeout)
            )
            if not promoted and _landed_elsewhere(pointer, run_id):
                raise _LandedElsewhereError
            try:
                measured = landing_measure.measure_run(
                    command=command,
                    tree=measured_tree,
                    base_sha=primary,
                    head_sha=measured_head,
                    base_tree=base_tree,
                    logs_dir=logs,
                    test_files=_landing_test_files(pointer, changed, measured_tree),
                    timeout=timeout,
                    stored=stored,
                    project=project,
                    changed=changed,
                    config=config,
                )
                measured = replace(
                    measured,
                    head=head,
                    population={
                        **measured.population,
                        "main_sha": primary,
                        "candidate_sha": measured_head,
                    },
                )
            except (OSError, ValueError) as exc:
                measured = landing_measure.Measurement(
                    command=command,
                    suite={},
                    population={},
                    unreadable=(f"the measurement raised {type(exc).__name__}: {exc}"),
                    head=head,
                    project=project,
                )
    except landing_measure.BaseCheckoutError as exc:
        measured = landing_measure.Measurement(
            command=command,
            suite={},
            population={},
            unreadable=f"the base checkout could not be built: {exc}",
            head=head,
            project=project,
        )
    if request is None:
        _record_suite_evidence(run_id, measured, promoted=promoted)
    return measured


def run_measurement_request(path: Path) -> None:
    """Run the comparisons inside their scheduler allocation, never the fleet."""
    from reckon._store import write_json_atomically

    request = json.loads(path.read_text(encoding="utf-8"))
    # The outer job owns the allocation. Its arms run there rather than
    # submitting further scheduler jobs and waiting for a second allocation.
    config = dict(request["config"])
    config["landing"] = {**config.get("landing", {}), "measure_partition": ""}
    request["config"] = config
    os.environ["PYTHONPATH"] = "."
    measured = _measure_delivery(
        request["pointer"],
        request["run_id"],
        request["head"],
        request["pointer"]["repo"],
        request["stored"],
        promoted=True,
        request=request,
    )
    write_json_atomically(
        path.parent / "result.json", replace(measured, execution="slurm").as_dict()
    )


def _scheduled_measurement(
    pointer: Mapping[str, Any],
    run_id: str,
    head: str,
    primary: str,
    stored: Sequence[Mapping[str, Any]],
    active: set[str],
) -> landing_measure.Measurement | None:
    """Adopt a persisted job, or submit once when the configured width allows."""
    from reckon._store import write_json_atomically

    job = pointer.get("landing_measurement_job")
    if isinstance(job, Mapping) and job.get("status") in {"pending", "complete"}:
        try:
            measured = landing_measure.read_measurement_job(job)
        except landing_measure.MeasurementJobError:
            raise
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # An unavailable scheduler says nothing about job completion.
            # Keep its slot occupied and try the observation on a later pass.
            return None
        if measured is None:
            return None
        active.discard(run_id)
        runs._mutate_pointer(
            run_id,
            lambda record: {
                **record,
                "landing_measurement_job": {**job, "status": "complete"},
            },
        )
        if (
            job["head"] == head
            and job["command"] == pointer.get("suite_command")
            and (
                job["primary"] == primary
                or _moved_primary_keeps_the_measurement(
                    pointer, job["primary"], primary
                )
            )
        ):
            _record_suite_evidence(run_id, measured)
            return measured
        # A finished job for another head still releases its slot, but cannot
        # stand as evidence about the delivery now being judged.
    if len(active) >= landing_measure.measurement_width():
        return None
    config = _measure_config(pointer)
    directory = runs.run_dir(run_id) / "landing-measure" / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=True)
    request_path = directory / "request.json"
    request = {
        "run_id": run_id,
        "head": head,
        "primary": primary,
        "pointer": dict(pointer),
        "stored": list(stored),
        "config": dict(config),
        "logs_dir": str(directory),
    }
    write_json_atomically(request_path, request)
    # Four comparison arms plus isolated repeats share one bounded job. The
    # stored arm rates widen its bound when previous populations took longer.
    elapsed = max(
        (
            sum(
                float(arm.get("elapsed_seconds") or 0)
                for arm in landing_measure._arm_records(record)
            )
            for record in stored
        ),
        default=0,
    )
    bound = max(_measure_timeout(pointer) * 10, elapsed * 2)
    job = landing_measure.submit_measurement(request_path, config=config, bound=bound)
    runs._mutate_pointer(
        run_id, lambda record: {**record, "landing_measurement_job": job}
    )
    active.add(run_id)
    return None


def _promoted_candidate(
    pointer: Mapping[str, Any], run_id: str, head: str, tree: str
) -> str:
    """Measure publication after completion has committed its records."""
    from reckon.crew import merge

    primary = _primary_revision(pointer)
    command = str(pointer.get("suite_command") or "").strip()
    measured = _stored_measurement(run_id, head, command, primary, pointer)
    if measured is not None:
        candidate = merge.build_merge_commit(
            Path(str(pointer.get("repo") or tree)),
            primary,
            head,
            "merge: integrate the measured delivery",
            "Combine the reviewed delivery with publication records that leave "
            "the measured product unchanged.",
        )
        _record_suite_evidence(
            run_id,
            replace(
                measured,
                population={
                    **measured.population,
                    "main_sha": primary,
                    "candidate_sha": candidate,
                },
            ),
            promoted=True,
        )
        return candidate
    if (_measure_config(pointer).get("landing") or {}).get("measure_partition"):
        raise CrewError(
            "the primary product changed during promotion; a new scheduler "
            "measurement is required before publishing the candidate"
        )
    measured = _measure_delivery(pointer, run_id, head, tree, promoted=True)
    refusal = _measure_guard(measured)
    if refusal:
        raise CrewError(refusal)
    return str(measured.population["candidate_sha"])


def _measure_guard(measured: Any) -> str:
    """The refusal a measurement states, or "" when the run may land."""
    if measured is None:
        return "landing measure unreadable: the run has no population measurement"
    if measured.unreadable:
        return f"landing measure unreadable: {measured.unreadable}"
    if not measured.real:
        return ""
    details = []
    for test_id in measured.real:
        results = measured.isolated.get(test_id) or {"base": [], "head": []}
        details.append(
            f"{test_id} failed {sum(results['head'])}/{len(results['head'])} at head "
            f"and {sum(results['base'])}/{len(results['base'])} at base"
        )
    logs = [
        str(arm["log_path"])
        for pair in (measured.suite, measured.population)
        for name in ("base", "head")
        if isinstance(arm := pair.get(name), Mapping) and arm.get("log_path")
    ]
    return (
        "added suite regression: "
        + "; ".join(details)
        + ("; logs: " + ", ".join(logs) if logs else "")
    )


#: The guard a run's own evidence raises when its suite is red or unreadable.
#: Every arm below states it, because a coordinator reads one guard per run and
#: the one thing they share is that the run's own gate did not come back clean.
RED_SUITE_GUARD = "red-suite"


def _canonical_test_ids(values: Any) -> set[str]:
    """The test ids a suite arm lists, in the spelling every comparison uses."""
    from reckon.crew import review as review_module

    if not isinstance(values, list):
        return set()
    return {
        review_module.canonical_node_id(str(test_id).strip())
        for test_id in values
        if str(test_id).strip()
    }


def _plain_int(value: Any) -> int | None:
    """A recorded integer as an int, or None when the field holds anything else.

    ``True`` is an ``int`` in Python, so a boolean written where a status
    belongs would read as 1 and pass a check that only tested the type.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _after_suite_arm(after: Any) -> tuple[Mapping[str, Any] | None, str]:
    """The run's head suite observation, and why it cannot be read when it cannot.

    A head arm that is absent, that does not declare its run complete, or that
    records no exit status states no gate result at all, so it is no evidence
    rather than clean evidence.
    """
    if not isinstance(after, Mapping):
        return None, (
            f"{RED_SUITE_GUARD}: the manifest records no after_suite observation "
            "of the delivered head, so the run states no gate result"
        )
    if after.get("completed") is not True:
        return None, (
            f"{RED_SUITE_GUARD}: the manifest's after_suite does not declare "
            f"completed: true (it records {after.get('completed')!r}), so its "
            "list of failures is not the set it measured"
        )
    exit_status = after.get("exit_status")
    if _plain_int(exit_status) is None:
        return None, (
            f"{RED_SUITE_GUARD}: the manifest's after_suite records no "
            "exit_status, so the run states no gate result"
        )
    return after, ""


def _after_suite_exit_guard(after: Mapping[str, Any]) -> str:
    """The refusal a head arm's own exit status raises, or "" when it passed."""
    exit_status = _plain_int(after.get("exit_status"))
    if exit_status in (None, 0):
        return ""
    return (
        f"{RED_SUITE_GUARD}: the manifest's after_suite exit status is "
        f"{exit_status}, so the run's own gate did not pass"
    )


def _after_suite_added_guard(
    after: Mapping[str, Any], baseline: Mapping[str, Any] | None
) -> str:
    """The refusal a failure id the baseline does not name raises, or ""."""
    added = _canonical_test_ids(after.get("failure_ids")) - _canonical_test_ids(
        baseline.get("failure_ids") if isinstance(baseline, Mapping) else None
    )
    if not added:
        return ""
    return (
        f"{RED_SUITE_GUARD}: the manifest's after_suite names failures its "
        "baseline_suite does not: " + ", ".join(sorted(added))
    )


def _suite_arms(pointer: Mapping[str, Any]) -> Mapping[str, Any]:
    """The suite arms a delivered manifest records, read once for both readers.

    The refusal reads these arms and the refusal's stamp digests them, so one
    pass reads the manifest once: two reads could see two versions of a file a
    coordinator is rewriting, and the refusal would then be stamped with
    evidence it was not judged on. An arm recorded as anything other than a
    mapping is kept here so that rewriting it releases the refusal.

    A manifest recording neither arm states nothing: a run whose node armed no
    suite pair has only the landing pass's own measurement to speak for it, and
    promotion already refuses a gate it cannot read.
    """
    from reckon.crew.promotion_checks import _fresh_manifest

    manifest = _fresh_manifest(pointer)
    if manifest is None:
        return {}
    return {
        name: manifest[name]
        for name in ("baseline_suite", "after_suite")
        if manifest.get(name) is not None
    }


def _manifest_suite_guard(arms: Mapping[str, Any]) -> str:
    """The refusal a run's own recorded suite pair raises, or "" when it is clean.

    The delivered manifest states the gate the worker ran at its base and at its
    head, and that record is the run's own account of whether its suite came
    back clean. An arm that cannot be read, a nonzero exit, or a failure id the
    baseline arm does not name each leave the run to its coordinator instead of
    promoting it.
    """
    if not arms:
        return ""
    after, unreadable = _after_suite_arm(arms.get("after_suite"))
    if unreadable:
        return unreadable
    baseline = arms.get("baseline_suite")
    return _after_suite_exit_guard(after or {}) or _after_suite_added_guard(
        after or {}, baseline if isinstance(baseline, Mapping) else None
    )


def _review_added_failure_guard(review: Mapping[str, Any] | None) -> str:
    """The refusal a review's own suite comparison raises, or "".

    A review that measured failures its base did not is saying the delivered
    head is red, however its scores read.
    """
    if not isinstance(review, Mapping):
        return ""
    count = _plain_int(review.get("added_failure_count"))
    if count is None or count <= 0:
        return ""
    ids = review.get("added_failure_ids")
    named = (
        ": " + ", ".join(str(test_id) for test_id in ids)
        if isinstance(ids, list) and ids
        else ""
    )
    return (
        f"{RED_SUITE_GUARD}: the run's review records added_failure_count "
        f"{count}{named}, so the review measured failures its base did not"
    )


def _undisposed_finding_guard(review: Mapping[str, Any] | None) -> str:
    """The refusal a finding nobody has answered raises, or "".

    Only a finding that withholds the landing refuses it: one that declares the
    blocking severity, and one that declares nothing at all, since an unreadable
    severity is a judgement the reviewer left unstated. A finding the reviewer
    declared non-blocking is carried forward unjudged — the landing records it
    as deferred once the run lands, and it stays open work from there — so it
    does not hold the run.

    The split is the deferral writer's, not a second reading of the severities:
    :func:`reckon.crew.findings.undisposed_finding_indexes` names both halves, so
    the guard admits exactly the findings that writer records and refuses the
    rest, and the index the refusal prints is the index the writer keys against.
    """
    from reckon.crew import findings as findings_module
    from reckon.crew import review as review_module

    if not isinstance(review, Mapping):
        return ""
    findings = findings_module._record_findings(review)
    open_rows: list[str] = []
    for index in findings_module.undisposed_finding_indexes(
        review, holding_landing=True
    ):
        finding = findings[index]
        severity = review_module.declared_severity(finding)
        path = str(finding.get("path") or "").strip()
        line = str(finding.get("line") or "").strip()
        anchor = f"{path}:{line}" if path and line else path or f"finding {index}"
        open_rows.append(f"{anchor} ({severity or 'unstated severity'})")
    if not open_rows:
        return ""
    return (
        f"{RED_SUITE_GUARD}: the run's review carries blocking findings with no "
        "disposition: " + "; ".join(open_rows)
    )


def _red_suite_guard(arms: Mapping[str, Any], review: Mapping[str, Any] | None) -> str:
    """The refusal the run's own evidence raises, or "" when it may be judged."""
    return (
        _manifest_suite_guard(arms)
        or _review_added_failure_guard(review)
        or _undisposed_finding_guard(review)
    )


def _suite_evidence_stamp(review_stamp: str, arms: Mapping[str, Any]) -> str:
    """Extend a review stamp with the suite arms a refusal was judged on.

    A refusal holds while the record that made it is unchanged, and the suite
    arms live in the manifest rather than the review. Without them in the
    stamp, a coordinator who records the missing observation would leave the
    refusal standing over evidence that had already moved.
    """
    if not arms:
        return review_stamp
    from reckon.crew.plan_review import _digest

    return review_stamp + ":" + _digest(dict(arms))


def _landing_beat(pointer: Mapping[str, Any], run_id: str) -> str:
    """Tick the landed node's section through the ops a coordinator uses.

    Returns "" when the beat was recorded or the run names no plan section, and
    the refusal otherwise. The evidence anchor is the node's own fragment path,
    so a reader follows the closing comment to the record the worker wrote.
    """
    node = pointer.get("node") if isinstance(pointer.get("node"), Mapping) else {}
    plan = str(node.get("plan") or "").strip()
    section = str(node.get("section") or "").strip()
    project = str(pointer.get("project") or "").strip()
    repo = str(pointer.get("repo") or "").strip()
    if not (plan and section and project and repo):
        return ""
    from datetime import UTC, datetime

    from reckon import _store
    from reckon.mcp_edit_plan import _edit_plan_tool

    anchor = str(node.get("id") or run_id)
    record_href = f"/{project}/evidence/archive/{plan}-landed.html#{anchor}"
    body = (
        f"<p>Landed {run_id} on the measured suite pair; "
        f'<a href="{record_href}">evidence</a></p>'
    )
    ops = [
        {
            "op": "append_evidence",
            "plan": plan,
            "anchor": anchor,
            "title": f"{section} landed",
            "body": f"<p>Run <code>{run_id}</code> landed after the landing pass "
            "measured its suite at base and head.</p>",
        },
        {"op": "set", "path": f"section_declarations.{section}", "value": "done"},
        {
            "op": "append",
            "target": "comments",
            "section": section,
            "item": {
                "id": f"c-close-{section}",
                "who": "reckon-build",
                "when": datetime.now(UTC).isoformat(),
                "body": body,
            },
        },
    ]
    _data, version = _store.read_plan(project, plan, repo)
    try:
        result = _edit_plan_tool(
            project,
            plan,
            expected_version=version,
            ops=ops,
            checkout_path=repo,
        )
    except (OSError, ValueError) as exc:
        return f"landing beat refused: {type(exc).__name__}: {exc}"
    if not result.get("ok"):
        return f"landing beat refused: {result.get('error') or 'unknown refusal'}"
    return ""


#: Why the land step waives the implementation fraction move. The pass ticks the
#: run's section done in ``_landing_beat``, and only after the merge this step
#: makes, so at the moment the step completes the fraction has not moved yet
#: and ``crew complete`` refuses a passing implement run without this. Recorded
#: on the ledger row of every run the pass lands.
LANDING_BEAT_REASON = (
    "the admission landing pass advances the run's plan section in its landing "
    "beat, after this merge; the impl has not moved at the moment of completion"
)


def _crew_step(*arguments: str) -> list[str]:
    """Run one crew operation through the bounded fleet subprocess."""
    from reckon.nested_launch import shim_directory

    return [
        str(shim_directory() / "srun"),
        "--overlap",
        "--ntasks=1",
        sys.executable,
        "-c",
        "from reckon.cli import main; main()",
        "crew",
        *arguments,
    ]


def _land_command(run_id: str) -> list[str]:
    """Complete the run before measuring and publishing its final candidate."""
    record = _measurement_record(run_id) or {}
    candidate = str((record.get("population") or {}).get("candidate_sha") or "")
    return _crew_step(
        "complete",
        "--run",
        run_id,
        "--gate",
        "passed",
        *[
            token
            for revision in _delivered_revisions(run_id)
            for token in ("--commit", revision)
        ],
        "--candidate",
        candidate,
        "--no-impl-change",
        LANDING_BEAT_REASON,
        "--land",
    )


def _delivered_revisions(run_id: str) -> tuple[str, ...]:
    """The revisions this run's manifest delivers, as the pass reads them.

    Read through the reader the pass used, because the two must agree on what a
    delivered run delivered: a land step that read the entries again would be a
    second reader, and a second reader is a second answer. Every entry the
    reader can place becomes a presented revision, so the promotion's boundary
    check covers the run's whole delivery and not only its tip.
    """
    try:
        pointer = runs.read_pointer(run_id)
    except (OSError, ValueError, CrewError):
        return ()
    classified = recovery.classify_pointer(pointer)
    revisions: list[str] = []
    for entry in classified.get("manifest_commits") or ():
        revision = _delivered_revision([entry])
        if revision and revision not in revisions:
            revisions.append(revision)
    return tuple(revisions)


def _land_timeout(run_id: str) -> float:
    """The wall-clock bound of one land step, from the run's own project config.

    The step runs the run's promotion gate inside the fleet allocation, so it
    is bounded by the same flight setting that bounds a measurement. A run whose
    pointer cannot be read is bounded by the default rather than left unbounded:
    a step with no bound is the stall the bound exists to prevent.
    """
    try:
        pointer = runs.read_pointer(run_id)
    except (OSError, ValueError, CrewError):
        return landing_measure.DEFAULT_MEASURE_TIMEOUT_SECONDS
    return _measure_timeout(pointer)


def _verified_measurement(
    run_id: str, candidate: str = ""
) -> landing_measure.Measurement:
    """Verify the measured delivery and primary before promotion can write."""
    from reckon.crew import merge

    record = _measurement_record(run_id)
    measured = landing_measure.Measurement.from_dict(record) if record else None
    guard = _measure_guard(measured)
    if guard:
        raise CrewError(guard)
    if not measured.population.get("candidate_sha") or not measured.population.get(
        "main_sha"
    ):
        raise CrewError("landing measure unreadable: no measured merge candidate")
    pointer = runs.read_pointer(run_id)
    repo = Path(str(pointer.get("repo") or ""))
    head, _tree = recovery._review_head_and_tree(pointer)
    if not recovery.same_revision(head, measured.head):
        raise CrewError("the measured delivery head changed before landing")
    measured_main = str(measured.population["main_sha"])
    primary = _primary_revision(pointer)
    moved = primary != measured_main
    if moved and not _moved_primary_keeps_the_measurement(
        pointer, measured_main, primary
    ):
        raise CrewError("the measured primary changed before completion")
    if (
        candidate
        and merge._rev_parse(repo, candidate) != measured.population["candidate_sha"]
    ):
        raise CrewError("the supplied candidate differs from the measured candidate")
    # The measured candidate is the merge of the measured primary and this
    # head. When the primary moved under a measurement that still stands, the
    # merge the landing will produce is built onto the current primary instead,
    # and that is the merge the change markers are judged against: what a
    # docs-only move cannot do is change the product, and that claim is only
    # worth making if the merge onto the new primary is read.
    if moved:
        merged_tree, conflicts = merge._merge_tree(repo, primary, head)
        if merge.refusing_conflicts(conflicts):
            from reckon.crew.recovery_repair_dispatch import dispatch_merge_repair

            dispatch_merge_repair(
                pointer,
                head=head,
                primary=primary,
                paths=merge.refusing_conflicts(conflicts),
            )
            raise CrewError(
                "the head no longer merges cleanly onto the primary: "
                + ", ".join(merge.refusing_conflicts(conflicts))
            )
        judged_base, judged_revision = primary, merged_tree
    else:
        judged_base = measured_main
        judged_revision = str(measured.population["candidate_sha"])
    markers = merge.change_markers(repo, judged_base, head)
    dropped = merge.surviving_change_refusals(repo, judged_revision, markers)
    if dropped:
        raise CrewError(
            "the measured merge dropped the reviewed change: " + "; ".join(dropped)
        )
    return measured


def _land(run_id: str) -> tuple[bool, str]:
    """Hand the measured delivery to completion, which owns publication."""
    from reckon.crew import merge

    try:
        _verified_measurement(run_id)
    except (OSError, ValueError, CrewError, merge.MergeError) as exc:
        return False, str(exc)

    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, environment.get("PYTHONPATH", "")) if part
    )
    timeout = _land_timeout(run_id)
    return _run_land_command(_land_command(run_id), environment, timeout)


def _run_land_command(
    command: list[str], environment: Mapping[str, str], timeout: float
) -> tuple[bool, str]:
    """Read the crew operation's receipt, preserving refusal and timeout details."""
    try:
        finished = _run_srun(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"the land step exceeded its {timeout:g} s bound and was stopped: "
            + " ".join(command)
        )
    output = (finished.stdout or "").strip()
    try:
        receipt = json.loads(output)
    except ValueError:
        receipt = {}
    if finished.returncode == 0 and receipt.get("ok") is True:
        return True, ""
    if isinstance(receipt, Mapping):
        steps = receipt.get("steps")
        if isinstance(steps, Mapping):
            for step in ("promote", "land", "release"):
                entry = steps.get(step)
                if isinstance(entry, Mapping) and entry.get("ok") is False:
                    return (
                        False,
                        f"{step}: {entry.get('error') or receipt.get('error') or 'refused'}",
                    )
        detail = receipt.get("error") or receipt.get("detail")
        if detail:
            return False, str(detail)
    return False, (
        finished.stderr or output or f"land exited {finished.returncode}"
    ).strip()


def _delivery_order(pointer: Mapping[str, Any]) -> tuple[int, str]:
    """Delivery order: the manifest write time, then the run id.

    The landing pass runs inside the admission loop, so a delivered run it
    cannot measure yet must not starve the ones delivered before it.
    """
    return (
        runs._manifest_mtime_ns(str(pointer.get("manifest_path") or "")),
        str(pointer.get("run_id") or ""),
    )


def land_once() -> dict[str, Any]:
    """Land each accepted delivery once and return obligations for refusals.

    Partitioned measurements are submitted without waiting, up to the
    configured in-flight width. Later passes adopt their persisted jobs and
    consume completed receipts. A delivery with reusable evidence can land in
    the same pass while those jobs remain pending. In-place measurements keep
    their single-measurement bound for callers outside the scheduler service.

    A head refused on an earlier pass is judged again as soon as the rule that
    refused it or the review it read has changed, so a run held by a rule since
    replaced does not stay held: the refusal is skipped only while the code
    stamp and the review record it judged are both unchanged. A refusal for an
    environmental failure — a measurement bound that was exceeded, or a base
    checkout that could not be built — is judged again on a doubling back-off
    with the code and the review unchanged, because neither would move while
    the host was merely busy. A manifest whose
    last commit entry names no revision is refused with that entry quoted and an
    obligation for the run's session, so the run is visible to a coordinator
    rather than dropped from the pass in silence. A delivered run missing a
    review it owes is recorded ``awaiting-review`` naming that tier and the
    review reflex's own state beside it, so waiting for a review is a verdict
    on the record rather than a run the pass never mentions. Every tier the run
    owes is read at its head, so a run that owes a figure review is not landed
    on its code review alone.

    The pass observes the docs tree once for every run it judges, through the
    same boundary an MCP read enters, and holds that observation for the whole
    pass so no run is judged from a different view than its neighbours. The
    pass reads no document the landing itself changes — it reads git, run
    records and stored reviews — so holding the boundary across a landing
    masks nothing.
    """
    from reckon.crew import pending_landing
    from reckon.crew.queue_admission import plan_read_scopes

    # A landing promoted but never merged is finished before new deliveries are
    # judged: its promotion already counts as landed everywhere else.
    lock = runs.crew_home() / "locks" / "landing-pass.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"landed": [], "obligations": [], "deferred": [], "busy": True}
        finished = pending_landing.finish_pending_landings()
        with plan_read_scopes():
            result = _land_deliveries()
    if finished:
        result["interrupted_landings"] = finished
    return result


def _land_deliveries() -> dict[str, Any]:
    landed: list[str] = []
    skipped: list[dict[str, str]] = []
    obligations: list[dict[str, str]] = []
    deferred: list[str] = []
    deferred_reasons: dict[str, str] = {}
    measured_this_pass = False
    live = sorted(runs.list_live(), key=_delivery_order)
    active_measurements = landing_measure.active_measurement_jobs(live)
    # One history read per project for the whole pass, not one per run: the
    # run directory holds thousands of entries on shared storage, and the pass
    # exists to stay off that cost.
    history: dict[str, list[dict[str, Any]]] = {}
    for pointer in live:
        if pointer.get("pending_landing_repair"):
            continue
        if (
            str(pointer.get("role") or (pointer.get("node") or {}).get("role") or "")
            != "implement"
        ):
            continue
        run_id = str(pointer.get("run_id") or "")
        project = str(pointer.get("project") or "")
        if not run_id or not project:
            continue
        classified = recovery.classify_pointer(pointer)
        if classified.get("manifest_status") != "complete":
            continue
        head, tree = recovery._review_head_and_tree(pointer)
        if not head:
            continue
        # A run the ledger already holds a row for has been promoted, and this
        # pass never lands a promoted run. Its landing is finished from the
        # pending-landing record that named the head it cited, so reading this
        # pointer's reviews would hold the run awaiting-review at whatever head
        # a resume moved it to — a revision no review read, and an obligation
        # nothing in this pass can answer.
        if _landed_elsewhere(pointer, run_id):
            skipped.append({"run_id": run_id, "status": "landed-elsewhere"})
            continue
        entries = classified.get("manifest_commits") or ()
        delivered = _delivered_revision(entries)
        code_stamp = _landing_code_stamp()
        if not delivered:
            guard = _unreadable_entry_guard(entries)
            if guard:
                previous = pointer.get("admission_landing")
                if not _unreadable_entry_holds(previous, head, guard, code_stamp):
                    _record_attempt(
                        run_id,
                        head=head,
                        guard=guard,
                        status="refused",
                        code_stamp=code_stamp,
                        previous=previous,
                    )
                    obligations.append(
                        {
                            "run_id": run_id,
                            "session": str(pointer.get("session") or ""),
                            "guard": guard,
                        }
                    )
            continue
        if not recovery.same_revision(head, delivered):
            continue
        # A coordinator's hold defers this delivery however clean its evidence
        # reads. It is skipped rather than refused: a refusal is a judgement
        # the pass makes and can retract, while a hold is the coordinator's
        # decision and ends only when it is released or the run delivers a new
        # head, which a hold a resume took stops covering.
        # The revision handed over is the head the pass resolved, not the
        # manifest's own spelling of it: a manifest may name its commit
        # abbreviated, and a hold taken against the full head must cover that
        # delivery rather than be read as naming a different one.
        hold_reason = delivery_hold_reason(pointer, head)
        if hold_reason:
            skipped.append({"run_id": run_id, "status": "held", "reason": hold_reason})
            continue
        owed = _owed_review_tiers(pointer)
        reviews: dict[str, dict[str, Any] | None] = {}
        stale, stale_tier = "", ""
        read_errors: dict[str, str] = {}
        unreadable: dict[str, list[dict[str, str]]] = {}
        for tier in owed:
            try:
                record, described = _review_for_tier(project, run_id, head, tree, tier)
            except _UnreadableFigureReviewError as broken:
                record, described = None, ""
                unreadable[tier] = broken.records
            except (OSError, ValueError) as exc:
                record, described = None, ""
                read_errors[tier] = f"{type(exc).__name__}: {exc}"
            reviews[tier] = record
            if described and not stale:
                stale, stale_tier = described, tier
        command = str(pointer.get("suite_command") or "").strip()
        primary = _primary_revision(pointer) if command else ""
        review_stamp = _owed_review_stamp(reviews)
        if primary and primary != head:
            review_stamp += ":" + primary
        suite_arms = _suite_arms(pointer)
        review_stamp = _suite_evidence_stamp(review_stamp, suite_arms)
        awaiting_tier = next(
            (
                tier
                for tier in owed
                if reviews.get(tier) is None and tier not in unreadable
            ),
            "",
        )
        stale_tier = stale_tier or awaiting_tier
        review = reviews.get(CODE_REVIEW_TIER)
        previous = pointer.get("admission_landing")
        if isinstance(previous, Mapping) and recovery.same_revision(
            str(previous.get("head") or ""), head
        ):
            if previous.get("status") == "attempting":
                guard = "automatic landing interrupted before a receipt"
                _record_attempt(
                    run_id,
                    head=head,
                    guard=guard,
                    status="refused",
                    code_stamp=code_stamp,
                    review_stamp=review_stamp,
                    previous=previous,
                )
                obligations.append(
                    {
                        "run_id": run_id,
                        "session": str(pointer.get("session") or ""),
                        "guard": guard,
                    }
                )
                continue
            if (
                previous.get("status") == "refused"
                and not pointer.get("merge_conflict")
                and _refusal_holds(previous, code_stamp, review_stamp)
            ):
                continue
        if stale or awaiting_tier or unreadable or read_errors:
            held_tier = stale_tier or awaiting_tier or next(iter(unreadable), "")
            held_tier = held_tier or next(iter(read_errors), "")
            _record_awaiting_review(
                run_id,
                pointer,
                head=head,
                code_stamp=code_stamp,
                review_stamp=review_stamp,
                tier=held_tier or CODE_REVIEW_TIER,
                stale_head=stale,
                read_error=read_errors.get(held_tier, ""),
                unreadable=unreadable.get(held_tier, ()),
            )
            continue
        incomplete = next(
            (
                tier
                for tier in owed
                if not _review_tier_is_complete(tier, reviews.get(tier))
            ),
            "",
        )
        if incomplete == FIGURE_REVIEW_TIER:
            findings = _undisposed_figure_findings(reviews.get(incomplete))
            if findings:
                _record_awaiting_review(
                    run_id,
                    pointer,
                    head=head,
                    code_stamp=code_stamp,
                    review_stamp=review_stamp,
                    tier=incomplete,
                    findings=findings,
                )
                continue
        if incomplete:
            guard = (
                "review acceptance: a blocking finding or an incomplete record"
                if incomplete == CODE_REVIEW_TIER
                else f"review acceptance: the stored {incomplete} review is incomplete"
            )
            _record_attempt(
                run_id,
                head=head,
                guard=guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
                previous=previous,
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": guard,
                }
            )
            continue
        # A run whose own evidence states a red or unreadable suite is left to
        # its coordinator: the landing measures a pair of its own, but a gate the
        # run itself did not come back clean on is a coordinator's decision, not
        # the admission service's. Refusing here also keeps the expensive
        # measurement off a run that cannot be promoted.
        red_suite = _red_suite_guard(suite_arms, review)
        if red_suite:
            _record_attempt(
                run_id,
                head=head,
                guard=red_suite,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
                previous=previous,
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": red_suite,
                }
            )
            continue
        # A head that conflicts with the primary branch cannot be landed at all,
        # so it is refused before the expensive measurement rather than after
        # it, and the refusal names the paths the next writer must resolve.
        conflict_primary = _primary_revision(pointer)
        conflicting = _merge_conflicts(
            {**pointer, "merge_primary_revision": conflict_primary}, head
        )
        if conflicting:
            from reckon.crew.recovery_repair_dispatch import dispatch_merge_repair

            primary = conflict_primary
            repair = dispatch_merge_repair(
                pointer, head=head, primary=primary, paths=conflicting
            )
            guard = (
                f"merge-conflict: the head conflicts with the primary branch at {primary}: "
                + ", ".join(conflicting)
            )
            _record_attempt(
                run_id,
                head=head,
                guard=guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
                previous=previous,
                remedy=str(repair["advice"]),
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": guard,
                }
            )
            continue
        if _landed_elsewhere(pointer, run_id):
            skipped.append({"run_id": run_id, "status": "landed-elsewhere"})
            continue
        command = str(pointer.get("suite_command") or "").strip()
        measured = (
            _stored_measurement(run_id, head, command, primary, pointer)
            if primary
            else None
        )
        if measured is not None:
            # The stored head is this head: reuse it, and re-apply it to the
            # pointer so a promoted pass still records what it measured on.
            _record_suite_evidence(run_id, measured)
        elif (_measure_config(pointer).get("landing") or {}).get(
            "measure_partition"
        ) or pointer.get("landing_measurement_job"):
            if project not in history:
                history[project] = _pass_history(live, project)
            try:
                measured = _scheduled_measurement(
                    pointer,
                    run_id,
                    head,
                    primary,
                    history[project],
                    active_measurements,
                )
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                job = pointer.get("landing_measurement_job")
                if isinstance(job, Mapping):
                    active_measurements.discard(run_id)
                    runs._mutate_pointer(
                        run_id,
                        lambda record, job=job: {
                            **record,
                            "landing_measurement_job": {**job, "status": "failed"},
                        },
                    )
                measured = landing_measure.Measurement(
                    command=command,
                    suite={},
                    population={},
                    head=head,
                    unreadable=str(exc),
                )
            if measured is None:
                deferred.append(run_id)
                deferred_reasons[run_id] = (
                    "the measurement job is pending or its in-flight width is full"
                )
                continue
        elif command and measured_this_pass:
            deferred.append(run_id)
            deferred_reasons[run_id] = (
                "another delivery was already measured in this pass, so its "
                "suite pair was not run a second time"
            )
            continue
        else:
            if project not in history:
                history[project] = _pass_history(live, project)
            stored = history[project]
            try:
                measured = _measure_delivery(pointer, run_id, head, tree, stored)
            except _LandedElsewhereError:
                skipped.append({"run_id": run_id, "status": "landed-elsewhere"})
                continue
            measured_this_pass = measured_this_pass or measured is not None
            if measured is not None:
                # A run measured in this pass is part of its project's history
                # for the runs judged after it.
                stored.append(measured.as_dict())
        measure_guard = _measure_guard(measured)
        if measure_guard:
            _record_attempt(
                run_id,
                head=head,
                guard=measure_guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
                previous=previous,
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": measure_guard,
                }
            )
            continue
        if (
            measured is not None
            and primary
            and _primary_revision(pointer) != primary
            and not _moved_primary_keeps_the_measurement(
                pointer, primary, _primary_revision(pointer)
            )
        ):
            deferred.append(run_id)
            deferred_reasons[run_id] = (
                "the primary moved during the measurement by a commit that "
                "changes the measured inputs, so the pair was taken against a "
                "product the landing will no longer produce"
            )
            continue
        if _landed_elsewhere(pointer, run_id):
            skipped.append({"run_id": run_id, "status": "landed-elsewhere"})
            continue
        # The review's non-blocking findings do not hold the landing, and the
        # finding a reviewer raised must not vanish with the pointer that
        # carried it. Each one is deferred only once the landing has succeeded:
        # a refusal here leaves the run unlanded, and recording a deferral would
        # say a landing happened that did not.
        try:
            _record_attempt(
                run_id,
                head=head,
                guard="crew land",
                status="attempting",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
                previous=previous,
            )
        except CrewError:
            if _landed_elsewhere(pointer, run_id):
                skipped.append({"run_id": run_id, "status": "landed-elsewhere"})
                continue
            raise
        try:
            accepted, guard = _land(run_id)
        except CrewError:
            if _landed_elsewhere(pointer, run_id):
                skipped.append({"run_id": run_id, "status": "landed-elsewhere"})
                continue
            raise
        except (OSError, ValueError) as exc:
            accepted, guard = False, f"{type(exc).__name__}: {exc}"
        if (
            not accepted
            and "no live run" in guard
            and _landed_elsewhere(pointer, run_id)
        ):
            skipped.append({"run_id": run_id, "status": "landed-elsewhere"})
            continue
        if accepted:
            landed.append(run_id)
            try:
                from reckon.crew import review_dispositions as dispositions_module

                dispositions_module.record_landed_deferrals(
                    project, run_id, reviewed_head_sha=head
                )
            except (OSError, ValueError) as exc:
                # The landing stands; only the record of what the reviewer
                # raised is missing, so the run is reported and not unlanded.
                obligations.append(
                    {
                        "run_id": run_id,
                        "session": str(pointer.get("session") or ""),
                        "guard": (
                            "the run landed but its review's non-blocking "
                            "findings could not be recorded as deferred: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    }
                )
            beat_refusal = _landing_beat(pointer, run_id)
            if beat_refusal:
                obligations.append(
                    {
                        "run_id": run_id,
                        "session": str(pointer.get("session") or ""),
                        "guard": beat_refusal,
                    }
                )
        else:
            guard = guard or "crew land refused without a guard"
            remedy = _landing_remedy(pointer, guard)
            _record_attempt(
                run_id,
                head=head,
                guard=guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
                previous=previous,
                remedy=remedy,
            )
            refused = {
                "run_id": run_id,
                "session": str(pointer.get("session") or ""),
                "guard": guard,
            }
            if remedy:
                refused["remedy"] = remedy
            obligations.append(refused)
    result = {"landed": landed, "obligations": obligations, "deferred": deferred}
    if deferred_reasons:
        result["deferred_reasons"] = deferred_reasons
    if skipped:
        result["skipped"] = skipped
    return result
