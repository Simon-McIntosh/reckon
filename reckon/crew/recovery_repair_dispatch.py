# ruff: noqa: I001, UP035
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from reckon import review_tiers
from reckon.crew import runs
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew.node import (
    CrewError,
)
from reckon.crew.reports import (
    parse_manifest,
)
from reckon.crew.runs import (
    _mutate_pointer,
    _utc_now,
    list_live,
    read_pointer,
)


def _is_repair_node(record: Mapping[str, Any]) -> bool:
    """Whether a run was minted by the repair composer.

    A repair carries the composer's own node-id prefix, so a review of it can be
    recognised without consulting the dispatch record. The chain this closes is
    the reason it exists: a finding on a repair opened another repair, up to a
    fourth round on one node, and each round inherited the parent's whole fence.
    """
    node = record.get("node") or {}
    return str(node.get("id") or "").startswith(repair_module.REPAIR_NODE_PREFIX)


def _repair_source_refusal(record: Mapping[str, Any]) -> str:
    """Why this run's findings open no repair, or empty when they may.

    Only an implement run is repaired. A review run and a repair run are both
    excluded on identity rather than on role, because their findings describe
    the record of a prior round rather than source a repair could edit; an
    investigate or a test run is excluded on its role, because its findings are
    about what it investigated or measured rather than about code it may change.
    """
    if _is_review_node(record) or _pointer_role(record) == REVIEW_ROLE:
        return "the run is itself a review, so it carries no repair round"
    if _is_repair_node(record):
        return "the run is itself a repair, so its findings open no further repair"
    role = _pointer_role(record)
    if role != REPAIR_SOURCE_ROLE:
        return (
            f"the run's role {role or '(unset)'} is not {REPAIR_SOURCE_ROLE}, "
            "so its findings open no repair"
        )
    return ""


def _repair_round_started_mtime_ns(
    record: Mapping[str, Any], round_id: str
) -> int | None:
    """The manifest baseline when this round's resume began, in nanoseconds.

    The round opens with the reviewed run's own resume write, which records the
    manifest's mtime as the attempt's baseline; the manifest's own mtime is
    compared against that baseline rather than against the second-resolution
    ``at`` stamp, because a manifest written just before the round can share the
    stamp's second and would then read as fresh. The pointer's own file mtime
    stands in when no baseline was recorded, the pointer having been written as
    the round opened. None when the record carries no outcome for this round, so
    a start belonging to another round is never used.
    """
    recorded = record.get(REPAIR_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return None
    if str(recorded.get("round_id") or "") != str(round_id or ""):
        return None
    baseline = record.get("manifest_baseline_mtime_ns")
    if isinstance(baseline, int) and not isinstance(baseline, bool):
        return baseline
    run_id = str(record.get("run_id") or "")
    if not run_id:
        return None
    try:
        return runs.pointer_path(run_id).stat().st_mtime_ns
    except OSError:
        return None


def _manifest_reports_round(text: str, round_id: str) -> str | None:
    """Which round's advice a manifest quotes: ``this``, ``other``, or None.

    A refusal may quote the advice verbatim, in which case the round token the
    advice opens with is present, or it may paraphrase the blocker and quote
    nothing, which reads the same as a manifest that simply predates the round.
    Telling those apart is the caller's job; this answers only whether the
    manifest names this round's token, a different round's token, or no token at
    all, so a manifest quoting an earlier round's advice is never mistaken for
    this round's refusal however the surrounding text is worded.
    """
    marker = REPAIR_ROUND_TOKEN_LINE
    rid = str(round_id or "")
    seen_other = False
    index = 0
    while True:
        found = text.find(marker, index)
        if found == -1:
            return "other" if seen_other else None
        after = text[found + len(marker) :]
        if rid and after.startswith(rid):
            return "this"
        seen_other = True
        index = found + len(marker)


def _reviewed_run_refused_the_round(record: Mapping[str, Any], round_id: str) -> bool:
    """Whether the reviewed run's own manifest refused this round's advice.

    A resumed turn can end in two ways the busy guard cannot tell apart: it can
    die mid-work, leaving the run's manifest untouched, or it can read the
    round's advice, refuse the dead end it names, and write a terminal manifest.
    Only the second is a dead end — a retry would re-send byte-identical advice
    into the same refusal — so the retry is suppressed exactly when the manifest
    already answers this round. The signal is not the wording: a terminal
    manifest written after this round's resume began is a refusal whether or not
    it copies the advice's opening token. The token corroborates: a manifest
    quoting this round's token is a refusal, and one quoting a different round's
    is not evidence about this round at all. A manifest that cannot be read, one
    carrying no recognised terminal status, or one the run held before the round
    opened is not a refusal, so the guard degrades toward the retry rather than
    suppressing it. The caller reaches this only for a round whose reviewed head
    has not moved, the moved head having moved the round on, so the head-unmoved
    half of the signal is settled before the manifest is read.
    """
    path = str(record.get("manifest_path") or "")
    if not path:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        parsed = parse_manifest(text, path=path)
    except Exception:  # noqa: BLE001 - an unreadable manifest is not a refusal
        return False
    if not manifest_status_is_terminal(parsed.get("status")):
        return False
    reported = _manifest_reports_round(text, round_id)
    if reported == "other":
        return False
    if reported == "this":
        return True
    started_ns = _repair_round_started_mtime_ns(record, round_id)
    if started_ns is None:
        return False
    try:
        written_ns = Path(path).stat().st_mtime_ns
    except OSError:
        return False
    return written_ns > started_ns


def _reviewed_run_is_busy(record: Mapping[str, Any]) -> str:
    """Why the reviewed run's own worker still holds its finding, or empty.

    A repair is composed for work nobody is doing. A reviewed run whose worker
    is live is already working; one whose worker was resumed on the same finding
    is doing exactly the repair the reflex would compose, and dispatching a
    second node for it is what a peer measured as a repair withdrawn about
    fourteen seconds after dispatch and re-fired on every sweep. Liveness is
    read from the run's own record — the supervisor's pid and the run's own
    worker-record pid, both checked on this host — and the resumed turn from the
    run's newest stream, which is a resume file only once a resume has run and
    holds an assistant record only while that turn is producing work. A resumed
    turn has ended once its stream's last record is the result line a finished
    turn writes, so a finished resume no longer reads as a turn in progress and
    does not hold the round busy forever. A run recording no process and
    carrying no unfinished resumed turn is left free to be repaired.
    """
    if runs.record_process_alive(record, process_alive) is True:
        return "the reviewed run's worker is live"
    if _worker_record_liveness(record) is True:
        return "the reviewed run's worker is live"
    found = _record_newest_stream(record)
    if (
        found is not None
        and found[0].name.startswith("resume-")
        and _stream_holds_assistant_record(found[0])
        and _newest_stream_last_record_type(record) != STREAM_RESULT_RECORD_TYPE
    ):
        return "the reviewed run has a resumed turn in progress"
    return ""


_RECORD_PATH_PREFIXES = ("docs/evidence/", "docs/figures/")


def _within_fence(path: str, fence: Sequence[str] | None) -> bool:
    """Whether a path sits inside a fence the reviewed run was itself granted.

    A fence entry matches the path itself or the directory a finding's file lies
    under, so a finding naming ``docs/figures/x/run.json`` is inside a fence
    granting ``docs/figures/x``. A leading ``./`` is dropped so the two
    spellings of one path compare alike. An absent or empty fence grants nothing.
    """
    text = str(path or "").strip().removeprefix("./").rstrip("/")
    if not text:
        return False
    for granted in fence or ():
        entry = str(granted or "").strip().removeprefix("./").rstrip("/")
        if entry and (text == entry or text.startswith(entry + "/")):
            return True
    return False


def _is_record_path(path: str, *, fence: Sequence[str] | None = None) -> bool:
    """Whether a finding's path names the fleet's own record, not a source file.

    Run directories, manifests, gate logs and the review store all live outside
    the repository under review — an absolute path or a home-relative one is a
    record for that reason — and the evidence fragments and figures live inside
    it under their own subtrees. A path inside the reviewed run's own declared
    fence is the exception: that run was granted the subtree, so a finding under
    it is work however it is spelled and the record prefixes apply only outside
    the fence. Everything else is a repository source or test path a repair may
    be granted.
    """
    text = str(path or "").strip()
    if not text:
        return True
    if _within_fence(text, fence):
        return False
    if text.startswith("~") or Path(text).is_absolute():
        return True
    return text.startswith(_RECORD_PATH_PREFIXES)


def _repairable_scope(
    paths: Iterable[str], *, fence: Sequence[str] | None = None
) -> list[str]:
    """The repository source and test paths among a repair's finding paths.

    Duplicates collapse to their first occurrence and record paths are dropped,
    so the repair is never granted a record outside its run's fence — a run
    directory, a manifest, a gate log or a review-store path. A path inside the
    reviewed run's own fence is kept whatever its spelling, so a finding under
    the run's granted ``docs/figures/`` or ``docs/evidence/`` subtree is
    repairable rather than mistaken for the fleet's own record. Empty answers
    "every finding cites only run records or evidence documents", which the
    caller reads as no repair to dispatch. The population is the caller's: the
    decline decision passes the blocking findings' own cited paths, and the
    composed scope passes the node's write paths, so the two can never disagree
    about which paths are records.
    """
    scope: list[str] = []
    for path in paths:
        text = str(path or "").strip()
        if text and text not in scope and not _is_record_path(text, fence=fence):
            scope.append(text)
    return scope


def _sub_floor_dimensions_named(
    review: Mapping[str, Any], project: str, config: Mapping[str, Any] | None
) -> list[str]:
    """The review's dimensions under their declared floor, as ``name score/floor``.

    The floors live in the resolved flight config's ``gates.dimension_floors``,
    so a dimension is named only where the project declared a standard for it and
    the review scored below that standard. A config that cannot be resolved names
    no floor rather than failing the decline, because the decline is a
    bookkeeping outcome and a missing config is not a reason to withhold it.
    """
    from reckon.crew import review_dispositions

    try:
        resolved = _resolved_review_config(project, config) if project else config
        floors = review_dispositions.declared_dimension_floors(resolved)
    except Exception:  # noqa: BLE001 - an unresolved config names no floor
        return []
    return [
        f"{row['dimension']} {row['score']}/{row['floor']}"
        for row in review_dispositions.sub_floor_dimensions(review, floors)
    ]


def _non_blocking_repository_paths(
    review: Mapping[str, Any], fence: Sequence[str] | None
) -> list[str]:
    """The repository paths the round's non-blocking findings cite.

    A non-blocking finding is recorded rather than repaired, so it carries no
    round on its own; but one citing a repository path is work a coordinator may
    dispose by hand, and the decline-only reason names it so the coordinator
    sees what the blocking-only decision left on the table. Run records are
    dropped by the same path rule the scope is composed from, so the reason never
    names a manifest or a gate log as work.
    """
    return _repairable_scope(
        (
            str(finding.get("file") or "")
            for finding in repair_module.non_blocking_findings(review)
        ),
        fence=fence,
    )


def _decline_only_reason(
    review: Mapping[str, Any],
    blocking: Sequence[Mapping[str, Any]],
    *,
    fence: Sequence[str] | None,
    project: str,
    config: Mapping[str, Any] | None,
) -> str:
    """The reason a round whose blocking findings cite no repairable path declines.

    The decision is taken from the blocking findings alone — a non-blocking
    finding citing source cannot carry a round — so the reason states that
    decision and names the population it was taken from, rather than the wider
    claim that no finding at all cited a repository path, which reads as false
    whenever a non-blocking finding did. What the decision left un-acted is then
    named: each dimension the review scored under its declared floor, and each
    repository path a non-blocking finding cites, so a coordinator reading the
    round record knows what to dispose by hand.
    """
    count = len(blocking)
    parts = [
        "no blocking finding cites a repairable path, so the round is decline-only",
        f"no finding cites a repository path among the {count} blocking finding(s)",
    ]
    sub_floor = _sub_floor_dimensions_named(review, project, config)
    if sub_floor:
        parts.append("sub-floor dimension(s): " + ", ".join(sub_floor))
    non_blocking = _non_blocking_repository_paths(review, fence)
    if non_blocking:
        parts.append(
            "non-blocking finding(s) cite repository path(s): "
            + ", ".join(non_blocking)
        )
    return "; ".join(parts)


def _review_carried_head(review: Mapping[str, Any]) -> str:
    """The head a stored review read, as its own record carries it.

    The round a repair belongs to is keyed on the head the *review* read, not on
    the reviewed run's current tree head: the composer re-reads the store by the
    round's head, so passing the tree head would select a different record — or
    none — the moment the reviewed run moves past the revision the review speaks
    for.
    """
    _, _, _, head = review_module.carried_revision_pair(review)
    return str(head or "").strip()


def _record_repair_dispatch(
    run_id: str,
    *,
    status: str,
    reason: str,
    round_id: str = "",
    repair_run_id: str = "",
    backend: str = "",
    node_id: str = "",
    open_findings: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Write the reflex's repair outcome onto the reviewed run it acted for.

    A skip is recorded as loudly as a dispatch, for the same reason the review
    outcome is: a round that produced no repair is otherwise indistinguishable
    from one the reflex never considered. The round and the node it composed are
    recorded with the outcome, so the next sweep tells an attempt of this round
    from a later round the run has since moved to.

    The attempt count is read from the pointer at the moment of the write and
    returned, so a caller can record the value the run now carries rather than
    deriving its own. It counts every outcome written for the round, so a caller
    that caps how many times a round may be resumed reads it as that count. The
    count is keyed to ``round_id``: an outcome written for a round other than the
    one the pointer last carried starts the new round's count afresh, so a round
    the reflex has not yet written reads attempt 1 whatever an earlier round of
    the same run recorded, rather than continuing that round's count.

    A write under an opening status also advances the run-wide opened-round
    record, so a reader can tell a round that opened from one that was only ever
    refused. The count advances once per distinct round rather than once per
    attempt: the retry of a resumed turn writes the same round's id, so it moves
    the round's attempt figure and leaves the count where it was. A refusal does
    not touch the record at all, so the round in hand stays free to open.
    """
    if not run_id:
        return 0
    written: dict[str, int] = {}

    def record(pointer: dict[str, Any]) -> dict[str, Any]:
        prior = pointer.get(REPAIR_DISPATCH_FIELD)
        prior = prior if isinstance(prior, Mapping) else {}
        # The count belongs to the round, not to the run: a write for a round the
        # pointer did not last carry starts at one, so a run whose earlier round
        # recorded attempts does not make its next round begin part-way exhausted.
        same_round = str(prior.get("round_id") or "") == str(round_id or "")
        attempt = int(prior.get("attempt") or 0) + 1 if same_round else 1
        written["attempt"] = attempt
        if status in REPAIR_ROUND_OPENING_STATUSES and round_id:
            existing = pointer.get(REPAIR_ROUNDS_FIELD)
            opened = dict(existing) if isinstance(existing, Mapping) else {}
            if str(opened.get("round_id") or "") != round_id:
                opened["count"] = int(opened.get("count") or 0) + 1
                opened["round_id"] = round_id
            opened["attempts"] = attempt
            pointer[REPAIR_ROUNDS_FIELD] = opened
        pointer[REPAIR_DISPATCH_FIELD] = {
            "status": status,
            "reason": reason,
            "run_id": repair_run_id or None,
            "node_id": node_id or None,
            "round_id": round_id or None,
            "backend": backend or None,
            "at": _utc_now(),
            "attempt": attempt,
        }
        if open_findings is not None:
            pointer[REPAIR_DISPATCH_FIELD]["open_findings"] = [
                dict(row) for row in open_findings
            ]
        return pointer

    _mutate_pointer(run_id, record)
    return written.get("attempt", 0)


def _repair_resume_advice(
    composed: Mapping[str, Any], scope: Sequence[str], round_id: str
) -> str:
    """The advice a resume of the reviewed run carries for its composed round.

    The reviewed run's own worker already holds its worktree, its claim and the
    context the findings are about, so the repair reaches it as the composed
    brief — which names every blocking finding by id — together with the write
    scope the round's findings grant, the round's done-when and the negative
    control the composer declared. The findings are therefore answered by id in
    the reviewed run's own manifest, and no finding is left without an answer.

    The advice opens with the round id, so a worker's refusal that quotes the
    advice names the round it refused and a retry can tell it from a manifest
    quoting an earlier round's advice.
    """
    findings = list(composed.get("findings") or ())
    ids = ", ".join(str(finding.get("id") or "") for finding in findings)
    scope = [str(path) for path in scope]
    parts = [
        _repair_round_token(round_id),
        "",
        f"An independent review of this run found {len(findings)} blocking "
        f"finding(s) ({ids}). Answer each in this run.",
        "",
        str(composed.get("brief") or ""),
        "",
        REPAIR_ADVICE_SCOPE_LINE + (", ".join(scope) if scope else "none"),
        "",
        f"Done when: {composed.get('done_when') or ''}",
        f"Negative control: {composed.get('negative_control') or ''}",
    ]
    return "\n".join(parts).strip() + "\n"


def _fold_findings_into_round(
    project: str, run_id: str, *, head: str, node_id: str
) -> list[int]:
    """Record each open blocking finding of the round as folded into it.

    The repair the reflex composes *is* the round that answers the review's
    blocking findings, so each of them is recorded as folded into that round
    through the same disposition writer ``crew dispose`` takes: the round's node
    id, and the head the review read. Both records the round stands for are read
    at that head — the run's text review and, when the run has one, its figure
    review — and a finding's index is counted against the record it lives on,
    which is the index the writer keys on and the reader prints.

    A record that cannot be read, or a finding the writer refuses, is left
    unfolder rather than raising the sweep out: the finding then stays
    undisposed, which is the direction that keeps a repair taken as an answer it
    never gave. The indexes folded into the text record are returned in the
    record's order, so a caller can report what the round answered.
    """
    from reckon.crew import repair as repair_module
    from reckon.crew import review_dispositions as review_dispositions_module

    folded: list[int] = []
    for rubric in (None, review_module.FIGURE_REVIEW_RUBRIC):
        try:
            record = review_module.read_review(
                project, run_id, reviewed_head_sha=head or None, rubric=rubric
            )
        except (OSError, ValueError):
            continue
        if record is None:
            continue
        blocking = repair_module.blocking_finding_dispositions(record)
        for index, _finding, disposition_kind in blocking:
            if disposition_kind != "open":
                continue
            try:
                review_dispositions_module.record_finding_disposition(
                    project,
                    run_id,
                    index,
                    kind="folded",
                    node=node_id,
                    reviewed_head_sha=head or None,
                    rubric=rubric,
                )
            except (OSError, ValueError):
                continue
            if rubric is None:
                folded.append(index)
    return folded


def _fold_a_landed_round(
    record: Mapping[str, Any], *, project: str, run_id: str
) -> list[int]:
    """Fold a round's findings once its repair has moved the run's head.

    This is the moment the round is spent: the reflex reads this run's stored
    review as describing a revision the run no longer carries, so the commit
    that answered the round has landed and the review the new head draws must
    not be refused for findings that round already repaired. Recorded where the
    reflex itself is what resumed the round — the run's own repair-dispatch
    record names the round and says it was resumed — so a head moved by anything
    else leaves the findings undisposed and the refusal standing.

    A round still in flight is never folded here: while the run stands at the
    head the review read, its findings must keep reading as work, or the reflex
    could no longer retry a resume whose turn ended without answering it.
    """
    durable = _live_pointer_or_absent(run_id) or record
    recorded = durable.get(REPAIR_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return []
    if str(recorded.get("status") or "") != "resumed":
        return []
    node_id = str(recorded.get("node_id") or "")
    round_id = str(recorded.get("round_id") or "")
    head = round_id.rpartition("@")[2] if "@" in round_id else ""
    if not node_id or not head:
        return []
    if review_module.read_review(project, run_id, reviewed_head_sha=head) is None:
        return []
    return _fold_findings_into_round(project, run_id, head=head, node_id=node_id)


def _promoted_run_ids(project: str) -> set[str]:
    """The run ids the project's ledger holds, or empty when it cannot be read.

    A ledger that cannot be read is not evidence a repair was promoted, so the
    refusal degrades to "unknown" and the round stays free to be attempted.
    """
    from reckon import ledger as ledger_module

    try:
        return ledger_module.run_ids(project)
    except Exception:  # noqa: BLE001 - an unreadable ledger is not a promotion
        return set()


def _live_pointer_or_absent(run_id: str) -> Mapping[str, Any] | None:
    """The run's live pointer, or ``None`` when it has gone or cannot be read.

    ``runs.read_pointer`` raises ``CrewError`` for a pointer file that is
    missing, holds invalid JSON or does not hold an object. Every caller here
    reads a pointer only to *degrade* on its absence — a gone pointer means the
    run is settled, not that the sweep should stop — so the raise is turned into
    ``None`` at one place rather than guarding each read separately.
    """
    try:
        fresh = read_pointer(run_id)
    except CrewError:
        return None
    return fresh if isinstance(fresh, Mapping) else None


def _opened_repair_rounds(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """The run-wide record of repair rounds the reflex has opened, or empty.

    Read from the pointer afresh rather than from the entry-time mapping, because
    the sweep holds a pointer read before the round ran and a round opened on an
    earlier cadence must still be visible here. A pointer that has gone, or one
    that carries no record yet, answers empty, which leaves the round free to
    open — the same direction the other repair guards degrade in.
    """
    durable = _live_pointer_or_absent(str(record.get("run_id") or "")) or record
    opened = durable.get(REPAIR_ROUNDS_FIELD)
    return opened if isinstance(opened, Mapping) else {}


def _recorded_repair_dispatch(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """The last repair outcome recorded on the run's own pointer, or empty.

    Read afresh for the same reason the opened-round record is: a sweep that
    reconsiders the same round must see the escalation a previous cadence wrote
    so it records no second one. A gone pointer or a record carrying no field
    answers empty, which leaves the round free to act.
    """
    durable = _live_pointer_or_absent(str(record.get("run_id") or "")) or record
    prior = durable.get(REPAIR_DISPATCH_FIELD)
    return prior if isinstance(prior, Mapping) else {}


def _open_finding_names(review: Mapping[str, Any]) -> list[dict[str, str]]:
    """One row per blocking finding, naming the id an answer would target.

    An escalation is useful only if the coordinator can see what is still open,
    so each row carries the finding's stable id, its location and its text. The
    id is the same value a repair round answers by, so a coordinator can answer
    the escalated finding with the verbs that accept a finding id rather than
    re-reading the whole review.
    """
    rows: list[dict[str, str]] = []
    for finding in repair_module.blocking_findings(review):
        location = str(finding.get("file") or "").strip()
        line = str(finding.get("line") or "").strip()
        if location and line:
            location = f"{location}:{line}"
        rows.append(
            {
                "id": str(finding.get("id") or "").strip(),
                "location": location or line,
                "text": str(finding.get("text") or "").strip(),
            }
        )
    return rows


def _repair_in_flight(record: Mapping[str, Any], *, node_id: str, project: str) -> str:
    """The repair run already standing for this round, or empty.

    Recognition keys on the node id the composer minted, which is a pure
    function of the round — the run reviewed and the head read — so a redispatch
    of a failed attempt is recognised as the same node rather than composed as a
    second. A round stands while its repair's pointer is live; once that pointer
    is gone the run has either died or been promoted, and the ledger settles
    which: a promoted repair satisfies the round, while a repair that died with
    no ledger row leaves the round free to be attempted again.
    """
    recorded = record.get(REPAIR_DISPATCH_FIELD)
    if isinstance(recorded, Mapping) and str(recorded.get("node_id") or "") == node_id:
        standing = str(recorded.get("run_id") or "")
        if standing:
            if runs.pointer_path(standing).exists() and read_pointer(standing):
                return standing
            if project and standing in _promoted_run_ids(project):
                return standing
    for pointer in list_live(project=project or None):
        node = pointer.get("node") or {}
        if str(node.get("id") or "") == node_id:
            return str(pointer.get("run_id") or "")
    return ""


def _repair_launch_refusal(run_id: str, project: str) -> str:
    """Why the reviewed run must not be repaired at the moment of launch, or empty.

    The pointer is re-read immediately before the dispatch call, because the
    composition above takes time and a coordinator may promote the reviewed run
    in that window: four repairs were measured dispatched seconds before or
    after a promotion, and a repair holding a promoted run's manifest would
    rewrite a settled record. A pointer that has vanished is a run promotion has
    reconciled away or a run whose worker took it with it, and either way there
    is no live reviewed run to repair; a run the ledger already holds, or one
    whose re-read carries a promoted revision, is settled and skipped too.
    """
    fresh = _live_pointer_or_absent(run_id)
    if fresh is None:
        return "the reviewed run's live pointer is gone"
    if str(fresh.get("promoted_revision") or "").strip():
        return "the reviewed run is promoted"
    if project and run_id in _promoted_run_ids(project):
        return "the reviewed run is promoted"
    return ""


def _reviewed_run_suite_command(record: Mapping[str, Any]) -> str:
    """The suite command the repair inherits from the run it repairs.

    The repair's own review derives its added-failure count from the pair of
    suite observations its manifest carries, and it can only reconcile that pair
    against the reviewed run when both were measured with the same command. So
    the repair inherits the reviewed run's own recorded suite command, read from
    its pointer's ``suite_command``. A reviewed run that recorded none was
    unarmed, and its repair is unarmed too: the project's standing
    ``review.suite`` declaration is the project's own gate, not a measurement the
    reviewed run was ever taken with, so inheriting it would compare the repair's
    observation against one the reviewed run never made.

    A scoped declaration is read through the same resolution promotion applies,
    so the repair is handed the files the reviewed run was actually measured on
    rather than an unsubstituted placeholder; one that resolves to nothing names
    no command, and the repair is unarmed like the run it answers.
    """
    # Deferred: promotion_records reads the run record through this module's
    # siblings, so the import is made where it is used rather than at load.
    from reckon.crew.promotion_records import _scoped_suite_command

    return _scoped_suite_command(record)


def _config_carrying_suite(
    config: Mapping[str, Any], suite_command: str
) -> Mapping[str, Any]:
    """Return ``config`` carrying the given suite command in its gates block.

    A run's pointer records the suite command dispatch reads from
    ``config.gates.suite_command``, so a caller composing a run against another's
    recorded measurement supplies the inherited command here. The original
    mapping is left untouched, because the lane above already resolved against
    it and the caller owns it. An empty command returns the config unchanged, so
    an unarmed reviewed run's repair records no suite command rather than a blank
    that would overwrite the config's own value.
    """
    if not suite_command:
        return config
    gates = config.get("gates")
    merged = dict(config)
    merged["gates"] = {
        **(dict(gates) if isinstance(gates, Mapping) else {}),
        "suite_command": suite_command,
    }
    return merged


def dispatch_repair_for_run(
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the one repair a finding-bearing review round composes for itself.

    A stored review that carries findings is a review *round* — the run it read
    and the head it read — and its work is one node: three findings answered in
    one turn cost one dispatch, where three repairs cost three lanes and three
    merges against the same files. The composition itself lives in
    :mod:`reckon.crew.repair`, so this function and a coordinator reading the
    same record compose the same node rather than each deriving its own.

    The return value is the reflex's own report, not a command: ``dispatched``
    says a repair run is now in flight, ``node_id`` and ``round_id`` name what
    was composed, ``goal`` and ``write_paths`` carry the brief the repair was
    dispatched with, and ``reason`` explains a false. Nothing here is raised for
    an ordinary refusal: a scope, member, lane or budget refusal is *reported*
    and recorded against the run, because the caller is a sweep that must reach
    the rest of the fleet.

    Four guards keep the reflex off a run it must not touch, each measured
    against a live fleet failure: only an implement run's findings open a
    repair, and a repair's own findings open none; a run whose worker is live or
    resumed is left to its own worker; a round whose findings name only the
    reflex's own record — a manifest, a gate log, a review-store path — cites no
    repository path, so it composes no repair and is recorded decline-only; and
    the reviewed run's pointer is re-read before the repair is offered, so a run
    promoted in that window, or one whose pointer has gone, is refused.

    An unpromoted reviewed run whose worker has exited is repaired in place: the
    composed findings ride a resume of the reviewed run itself, which already
    holds the worktree and the commit claim a new node could only be refused
    for. A promoted run, or one whose live pointer has gone, is refused rather
    than dispatched: the reviewed run is settled, so there is no tree left to
    cut a repair against and the round is reported back with its reason. The
    resume is offered while the run's worker is not live, so a resumed turn that
    ends without answering is retried rather than leaving the round stuck, and
    the attempt count records each retry. The retry is bounded at
    REPAIR_RESUME_LIMIT resumes per round; a round resumed that many times
    without its head moving is recorded exhausted, so no sweep spends lane
    capacity on it again.
    """
    run_id = str(record.get("run_id") or "")
    refusal = _repair_source_refusal(record)
    if refusal:
        return {"run_id": run_id, "dispatched": False, "reason": refusal}
    project = str(record.get("project") or "")
    if not project:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "the run records no project to compose a repair against",
        }
    # A cheap presence check before the head resolution below costs a git
    # subprocess: a run with no review record on disk has no round to repair,
    # and the sweep reaches this for every live pointer on its cadence.
    _stored_path, stored = review_module.stored_record(project, run_id)
    if stored is None:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "no review is stored for this run",
        }
    busy = _reviewed_run_is_busy(record)
    if busy:
        return {"run_id": run_id, "dispatched": False, "reason": busy}
    review, review_error = _stored_review(record)
    if review is None or not _review_is_complete(review):
        # No stored review describes the run's current head, which for a round
        # this reflex resumed is the round landing: the head moved because its
        # findings were answered, so they are folded into it here rather than
        # left to refuse the review the new head draws. The read is taken only
        # when a review exists at a revision the run has moved past, so a run
        # with no review at all folds nothing.
        landed = (
            _fold_a_landed_round(record, project=project, run_id=run_id)
            if review is None
            else []
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "review_status": "unreadable" if review_error else "present",
            "folded_findings": landed,
            "reason": (
                f"the stored review could not be read: {review_error}"
                if review_error
                else "the stored review does not cover the run's current head"
            ),
        }
    # Fold the run's figure review at the same head into the record once, here,
    # so every reading below sees the figure review's findings as well as the
    # text review's. The three readings this pre-empts each decide from the
    # record in hand — whether any finding was raised, whether any of them
    # blocks, and which paths the round's scope grants — and a merge made after
    # any of them would leave that reading answering over the text review alone.
    # Read without the merge, a clean text review hides a blocking figure
    # finding: the round is never composed and the figure defect waits for a
    # coordinator, which is what the merge exists to prevent.
    review = repair_module.review_with_figure(review)
    findings = repair_module.review_findings(review)
    if not findings:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "the stored review carries no finding, so composes no repair",
        }
    fields = _review_dispatch_fields(record)
    round_id = repair_module.repair_round_id(review, reviewed_run_id=run_id)
    # The reflex opens a run's automatic repair rounds up to
    # AUTOMATIC_REPAIR_ROUND_LIMIT. A *later* head composes a different round,
    # and each one is repaired in turn until the budget is spent; a field run
    # measured the same node resumed on reviews scoring 82, 83 and 83, each a
    # new head drawing a new review and a new round, so the rounds are bounded
    # rather than open-ended. Once the budget is spent the run is escalated to
    # its coordinator exactly once, recorded with the earlier round, its
    # attempts, the new review's score and the findings still open, so a reader
    # sees what needs answering. A round already recorded as opened is the round
    # in hand, so its own retry (below) is untouched, and a round that was only
    # ever refused never entered the record at all, leaving it free to open.
    opened_rounds = _opened_repair_rounds(record)
    opened_count = int(opened_rounds.get("count") or 0)
    opened_round_id = str(opened_rounds.get("round_id") or "")
    if (
        opened_count
        and opened_round_id
        and opened_round_id != round_id
        and opened_count >= AUTOMATIC_REPAIR_ROUND_LIMIT
    ):
        earlier_attempts = int(opened_rounds.get("attempts") or 0)
        score = review.get("total")
        open_findings = _open_finding_names(review)
        reason = (
            f"the automatic repair budget is spent: {opened_count} round(s) "
            f"opened (latest {opened_round_id}, {earlier_attempts} attempt(s)) "
            f"and this review scores {score}; the run needs a coordinator"
        )
        prior = _recorded_repair_dispatch(record)
        already_recorded = str(
            prior.get("status") or ""
        ) == REPAIR_ESCALATED_STATUS and str(prior.get("round_id") or "") == str(
            round_id or ""
        )
        if not already_recorded:
            _record_repair_dispatch(
                run_id,
                status=REPAIR_ESCALATED_STATUS,
                reason=reason,
                round_id=round_id,
                open_findings=open_findings,
            )
        return {
            "run_id": run_id,
            "dispatched": False,
            "escalated": True,
            "round_id": round_id,
            "open_findings": open_findings,
            "reason": reason,
        }
    repo = str(record.get("repo") or "")
    if not repo:
        reason = "the run records no repository to dispatch a repair against"
        _record_repair_dispatch(
            run_id, status="refused", reason=reason, round_id=round_id
        )
        return {"run_id": run_id, "dispatched": False, "reason": reason}

    # The head the review read. The repair is cut from it so the reviewed head
    # is an ancestor of the repair's own tree: a repair dispatched at the branch
    # tip would answer findings against a revision the review never saw, and the
    # round's identity — the run at the head it read — would name a tree the
    # repair never stood on. A promoted run never reaches here (the launch
    # refusal above settles it), so this is the unpromoted case by construction.
    reviewed_head = _review_carried_head(review)
    # A later round's brief carries only the work since the previous round was
    # reviewed, not the whole run again: the last reviewed head is named by the
    # round id the reflex last opened, which is ``<run>@<head>``.
    review_diff = ""
    previous_head = opened_round_id.partition("@")[2]
    if (
        previous_head
        and reviewed_head
        and not same_revision(previous_head, reviewed_head)
    ):
        review_diff = (
            _diff_between(_review_tree(record), previous_head, reviewed_head) or ""
        )
    # The repair inherits the reviewed run's own recorded suite command, so its
    # review measures added failures against the same suite the reviewed run was
    # measured with. An unarmed reviewed run yields no command, and the repair is
    # unarmed like the run it repairs.
    inherited_suite = _reviewed_run_suite_command(record)
    composed = repair_module.compose_repair_for_run(
        project,
        run_id,
        reviewed_head_sha=reviewed_head or None,
        source_node=fields["source_node"],
        plan=str(fields["plan"]),
        section=str(fields["section"]),
        session=str(fields["session"]),
        time_budget=str(fields["time_budget"]),
        # The reviewed run's own fence is laid into the scope: its test paths
        # are the repair's gate, and a finding under the run's granted
        # ``docs/figures/`` or ``docs/evidence/`` subtree is work the run
        # already held rather than the fleet's own record.
        run_record=record,
        suite_command=inherited_suite,
        diff=review_diff,
    )
    if composed is None:
        # The composer returns None for a finding-bearing record in two cases,
        # told apart from the findings already read here so the recorded reason
        # names the non-blocking count rather than one string standing for both.
        # A round whose findings are all non-blocking has nothing that blocks to
        # answer, so it composes no repair. A finding with no readable severity
        # blocks, so such a round carries no unmarked finding to count; the
        # unmarked list below stays for the record and is empty here, and any it
        # did name would be listed by file and line so the record names what was
        # reported rather than repaired. The record is left on the reviewed run
        # as the round's outcome. Otherwise the record the composer re-read
        # differs from the one selected here, and the round is left for a
        # coordinator rather than dispatched from a stale parse.
        if not repair_module.blocking_findings(review):
            non_blocking = repair_module.non_blocking_findings(review)
            unmarked = repair_module.unmarked_findings(review)
            disposed = [
                (index, kind)
                for index, _, kind in repair_module.blocking_finding_dispositions(
                    review
                )
                if kind != "open"
            ]
            if disposed:
                names = ", ".join(f"index {index} ({kind})" for index, kind in disposed)
                reason = f"every blocking finding was disposed: {names}"
            else:
                reason = (
                    "the review round carried no blocking finding "
                    f"({len(non_blocking)} non-blocking finding(s))"
                )
            _record_repair_dispatch(
                run_id,
                status="decline-only",
                reason=reason,
                round_id=round_id,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "reason": reason,
                "unmarked_findings": [
                    {"file": finding["file"], "line": finding["line"]}
                    for finding in unmarked
                ],
            }
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "no repair composes for this review round",
        }
    node_id = str(composed["node_id"])
    round_id = str(composed["round_id"])
    run_fence = repair_module._run_fence(record)
    blocking = repair_module.blocking_findings(review)
    # The round is decline-only when no *blocking finding cites a repairable
    # path. The decision must read the blocking findings, the same population the
    # scope is composed from: reading every finding let a non-blocking finding
    # citing a path carry a round through whose blocking findings cited only the
    # fleet's own record, so the scope came out empty. The composed scope below
    # also carries the reviewed run's whole fence, granted so the repair can run
    # the reviewed gate, so a decision read from that scope is never empty for a
    # run holding a test path; the cited paths are the only place the findings'
    # own files appear, and they are read here. A path inside the run's own fence
    # is repairable whatever its spelling, so an in-fence figure or evidence
    # finding is not mistaken for the fleet's own record.
    if not _repairable_scope(
        (str(finding.get("file") or "") for finding in blocking), fence=run_fence
    ):
        reason = _decline_only_reason(
            review, blocking, fence=run_fence, project=project, config=config
        )
        _record_repair_dispatch(
            run_id,
            status="decline-only",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "node_id": node_id,
            "round_id": round_id,
            "reason": reason,
        }
    scope = _repairable_scope(composed["write_paths"], fence=run_fence)
    # A composed scope that filters to empty while blocking findings exist is a
    # dead end: the round would otherwise resume with the advice line "Write
    # scope for this round: none", which is no scope for work the review's
    # blocking findings named. Record it declined instead of resuming it.
    if not scope:
        reason = (
            "the composed write scope filtered to empty, so the round is decline-only"
        )
        _record_repair_dispatch(
            run_id,
            status="decline-only",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "node_id": node_id,
            "round_id": round_id,
            "reason": reason,
            "blocking_findings": len(blocking),
        }
    standing = _repair_in_flight(record, node_id=node_id, project=project)
    if standing:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "a repair for this review round is already standing",
            "repair_run_id": standing,
            "node_id": node_id,
            "round_id": round_id,
        }

    resolved = _resolved_review_config(project, config)
    try:
        from reckon import flight

        resolved = flight.select_local_backend(resolved)
    except Exception as exc:  # noqa: BLE001 - the configured lane is the reason
        reason = f"the local lane is unavailable: {exc}"
        _record_repair_dispatch(
            run_id,
            status="awaiting-lane",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "reason": reason,
        }

    owning_lane = str(record.get("backend") or "").strip()
    candidates = _review_lane_candidates(resolved, owning_backend=owning_lane)
    if not candidates:
        reason = _no_lane_reason(
            run_id, resolved, owning_backend=owning_lane, kind="repair"
        )
        _record_repair_dispatch(
            run_id,
            status="awaiting-lane",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "reason": reason,
        }
    backend = candidates[0]

    # An unpromoted reviewed run whose worker has exited still holds its own
    # worktree and its commit claim, so a repair node over the reviewed run's own
    # paths is refused at dispatch — measured on every sweep as a repair
    # withdrawn about fourteen seconds after it was dispatched and re-fired on
    # each cadence. The repair for such a run is the reviewed run itself: the
    # composed findings ride a resume of the run that already owns the worktree
    # and the claim, through the same entry point a hand-typed ``crew resume``
    # uses, and no second node is composed. A run whose worker is live or resumed
    # never reaches here (the busy guard above), and a promoted run or one whose
    # pointer has gone is refused below rather than dispatched.
    #
    # The round is in flight only while that worker is live: the busy guard above
    # returns for a live worker, so reaching this point means the resumed turn has
    # ended. If it ended without answering — the reviewed run's head has not moved
    # past the reviewed head, since a moved head no longer matches the stored
    # review — the round is resumed once more, and the attempt count recorded on
    # the run makes the retry visible. The retry is bounded: a round is resumed
    # at most REPAIR_RESUME_LIMIT times, the first plus the one retry, so a turn
    # that keeps ending without answering is exhausted rather than spending lane
    # capacity on every sweep forever. The count is read from the pointer's
    # durable record, so an entry-time mapping cannot stale-hold it.
    if not _repair_launch_refusal(run_id, project):
        from reckon.crew import resumption as resumption_module
        from reckon.crew.dispatch import BudgetHold, LanePaused

        durable = read_pointer(run_id) or {}
        recorded = durable.get(REPAIR_DISPATCH_FIELD)
        same_round = isinstance(recorded, Mapping) and str(
            recorded.get("round_id") or ""
        ) == str(round_id or "")
        prior = int((recorded or {}).get("attempt") or 0) if same_round else 0
        if (
            same_round
            and prior >= 1
            and _reviewed_run_refused_the_round(durable or record, round_id)
        ):
            reason = (
                "the ended turn refused this round's advice; a retry would "
                "re-send it into the same dead end"
            )
            attempt = _record_repair_dispatch(
                run_id,
                status="exhausted",
                reason=reason,
                round_id=round_id,
                node_id=node_id,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "exhausted": True,
                "node_id": node_id,
                "round_id": round_id,
                "attempt": attempt,
                "reason": reason,
            }
        if prior >= REPAIR_RESUME_LIMIT:
            reason = "the round was resumed twice without answering its findings"
            if same_round and str(recorded.get("status") or "") == "exhausted":
                return {
                    "run_id": run_id,
                    "dispatched": False,
                    "exhausted": True,
                    "node_id": node_id,
                    "round_id": round_id,
                    "attempt": prior,
                    "reason": reason,
                }
            attempt = _record_repair_dispatch(
                run_id,
                status="exhausted",
                reason=reason,
                round_id=round_id,
                node_id=node_id,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "exhausted": True,
                "node_id": node_id,
                "round_id": round_id,
                "attempt": attempt,
                "reason": reason,
            }

        advice = _repair_resume_advice(composed, scope, round_id)
        try:
            resumed = resumption_module._resume(
                run_id, record, config=config, launcher=launcher, advice=advice
            )
        except LanePaused as exc:
            gate = dict(exc.gate)
            reason = (
                str(gate.get("detail") or "").strip()
                or str(gate.get("reason") or "").strip()
                or f"the {backend} lane gate is {gate.get('state')!r}"
            )
            _record_repair_dispatch(
                run_id,
                status="lane-paused",
                reason=reason,
                round_id=round_id,
                node_id=node_id,
                backend=backend,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "error": "lane-paused",
                "backend": backend,
                "lane_gate": gate,
                "reason": reason,
            }
        except (BudgetHold, CrewError, OSError) as exc:
            reason = f"the reviewed run could not be resumed: {exc}"
            _record_repair_dispatch(
                run_id,
                status="refused",
                reason=reason,
                round_id=round_id,
                node_id=node_id,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "refused": True,
                "reason": reason,
            }
        attempt = _record_repair_dispatch(
            run_id,
            status="resumed",
            reason="the reviewed run was resumed with the composed findings as advice",
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "resumed": True,
            "node_id": node_id,
            "round_id": round_id,
            "attempt": attempt,
            "reason": "resumed the reviewed run with the composed findings as advice",
            "resumed_turn": resumed.get("turn"),
        }

    # The split above offered the resume, so reaching here means the refusal was
    # truthy: the reviewed run is promoted, or its live pointer has gone. Either
    # way there is no unpromoted tree to cut a repair against, so the round is
    # refused and reported rather than dispatched. The refusal is read once more
    # because the lane resolution above takes time and a coordinator may promote
    # the reviewed run in that window, and both reads yield the same reason.
    launch_refusal = _repair_launch_refusal(run_id, project)
    # A gone pointer has no run to record against, and the write path re-reads
    # the pointer under its lock, so recording only when one is still live keeps
    # the gone-pointer case from raising out of the reflex.
    if _live_pointer_or_absent(run_id) is not None:
        _record_repair_dispatch(
            run_id,
            status="refused",
            reason=launch_refusal,
            round_id=round_id,
            node_id=node_id,
            backend=backend,
        )
    return {
        "run_id": run_id,
        "dispatched": False,
        "refused": True,
        "reason": launch_refusal,
    }


def _sweeping_session(project: str | None) -> str:
    """The session whose follower this process serves, or empty.

    A sweep runs inside one session's follower, and the review it composes for
    a run belongs to that run's owning session: composing one for another
    session's run attributes a lane and a member to a coordinator that did not
    choose either, which is a project-wide sweep placing runs under someone
    else's runtime. The follower's registration names the session and the
    process that wrote it, so a registration written by this process is the
    identity. A process holding no registration — a hand-run sweep — has no
    session to confine to and returns empty, which the caller reads as no
    filter.
    """
    if not project:
        return ""
    for row in runs.list_followers(project):
        follower = row.get("follower") or {}
        if follower.get("pid") == os.getpid():
            return str(row.get("session") or "")
    return ""


def _sweep_review_tier(
    record: Mapping[str, Any], manifest_commits: Sequence[Any]
) -> str:
    """The review tier a scoring run resolves to, through promotion's resolver.

    The reflex must skip exactly the runs promotion's own gate would land
    without a review, so it reads the tier through the same
    :mod:`reckon.review_tiers` resolver promotion calls, on the same inputs —
    changed paths, changed lines, declared spec level and declared capability
    risk. Re-deriving the rule here is how a sweep and a promotion come to
    disagree about which run owes a review, and the disagreement is silent in
    both directions. A scope that cannot be measured is read as the fuller
    tier, so a run whose change is unreadable is still reviewed rather than
    skipped.
    """
    from reckon.crew import promotion

    commits = _canonical_commits(_review_tree(record), manifest_commits)
    try:
        return promotion._run_review_tier(
            str(record.get("run_id") or ""),
            record,
            commit_list=commits,
            root=str(record.get("repo") or "") or None,
        )
    except Exception:  # noqa: BLE001 - an unreadable scope owes the fuller review
        return review_tiers.FULL


from .recovery_liveness import (  # noqa: E402
    _record_newest_stream,
    _worker_record_liveness,
    process_alive,
)
from .recovery_review_dispatch import (  # noqa: E402
    AUTOMATIC_REPAIR_ROUND_LIMIT,
    REPAIR_ADVICE_SCOPE_LINE,
    REPAIR_DISPATCH_FIELD,
    REPAIR_ESCALATED_STATUS,
    REPAIR_RESUME_LIMIT,
    REPAIR_ROUNDS_FIELD,
    REPAIR_ROUND_OPENING_STATUSES,
    REPAIR_ROUND_TOKEN_LINE,
    REPAIR_SOURCE_ROLE,
    _diff_between,
    _no_lane_reason,
    _repair_round_token,
    _resolved_review_config,
    _review_lane_candidates,
)
from .recovery_review_subject import (  # noqa: E402
    _canonical_commits,
    _is_review_node,
    _review_dispatch_fields,
    _review_is_complete,
    _review_tree,
    _stored_review,
    same_revision,
)
from .recovery_stream import (  # noqa: E402
    manifest_status_is_terminal,
)
from .recovery_vocabulary import (  # noqa: E402
    REVIEW_ROLE,
)
from .recovery_wait import (  # noqa: E402
    STREAM_RESULT_RECORD_TYPE,
    _newest_stream_last_record_type,
    _stream_holds_assistant_record,
)
from .recovery_watch import (  # noqa: E402
    _pointer_role,
)
