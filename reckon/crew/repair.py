"""Compose the one repair node a finding-bearing review round produces.

A review that finds something has to become work, and the work is one node per
review *round* rather than one node per finding: a repair that answers three
findings in one turn costs one dispatch, where three repairs cost three lanes
and three merges against the same files. This module turns a stored review
record into that single node — its brief lists every finding that blocks under a
stable id, its write scope is the union of the paths those findings name and the
reviewed run's own fence, and its manifest contract obliges the repair to answer
every finding it declines with a reason. A finding that declares itself
non-blocking is left out of all three: it is recorded on the review rather than
become work, so a round of non-blocking findings alone composes no repair.

The identity of a round is the pair a review stands for: the run it reviewed and
the head it read. Composition is a pure function of that pair and the record, so
composing twice for the same round yields the same node id rather than a second
node. A redispatch of that node is therefore another attempt of one round, not a
new one — which is what lets a failed attempt be resumed without the round
being counted twice.

The finding vocabulary is shared with plan review rather than reinvented: a
finding carries a stable ``id`` and a response names an action (``acted`` or
``declined``) with a reason. Code-review findings are parsed without an id, so
this module mints one from the finding's own position in the frozen record; the
id is meaningful only against that record, which is what re-reading it returns.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from reckon._timestamps import parse_iso
from reckon.crew import review as review_module
from reckon.crew.plan_review import RESPONSE_ACTIONS

# The node-id prefix a composed repair carries, so a run can be recognised as
# the repair of a reviewed run without consulting the dispatch record. It sits
# beside the review prefix rather than sharing it: a repair that reviewed a
# review would otherwise be indistinguishable from a review of a review.
REPAIR_NODE_PREFIX = "repair-of-"

# The moment the write-time audit of a review run began reporting a stored
# finding that declares no severity. The audit reports; it does not refuse the
# write, so a finding that reached the store unmarked after this moment is
# still a finding a reviewer raised, and it is repaired as blocking either way.
# The moment only decides whether the record is flagged: a record stamped at or
# after it is one whose reviewer was told to restate the severity and did not,
# so the composed repair names the unmarked findings for the coordinator; a
# record stamped before it predates the instruction, where an absent severity
# was ordinary and carries no such flag.
UNMARKED_SEVERITY_AUDIT_STARTED_AT = "2026-10-01T07:14:36+00:00"

# The finding-id prefix. The id itself is derived from the finding's own file,
# line and text, so a constant cannot identify a record's findings and two
# findings that differ only in text cannot collide on one id. It is stable
# across re-reads because those three fields do not change once stored.
FINDING_ID_PREFIX = "f"

# The two answers a repair may give a finding, taken from the plan-review
# vocabulary rather than declared here. The response record validates against
# ``RESPONSE_ACTIONS``, so the words the brief teaches and the words the record
# accepts are one fact read once, and the brief cannot drift to a spelling the
# record would reject.
ACTED_ACTION, DECLINED_ACTION = RESPONSE_ACTIONS

# The write scope is declared from the review: the paths the findings name. The
# reviewed run's own fence is unioned in by the caller from the run's record,
# so a finding outside that fence is granted rather than refused.
_DEFAULT_TIME_BUDGET = "30m"

# The separator between the three fields a finding id is derived from. It is a
# NUL so no field value can forge a boundary: a file path or a line cannot
# contain it, so two distinct fields cannot join into the same material.
_ID_MATERIAL_SEPARATOR = "\x00"

# How many hexadecimal digits of the digest the id keeps. Ten hex digits is
# forty bits, far wider than any review's finding count, so a collision needs an
# adversary rather than a long review.
_ID_DIGEST_DIGITS = 10


def finding_id(finding: Mapping[str, Any]) -> str:
    """Return the stable id of one finding, derived from its own content.

    The id is the fixed prefix followed by a digest over the finding's file,
    line and text, so it is a function of the finding rather than of its
    position: two findings that differ only in their text get different ids, and
    a scheme that returned one constant for every finding is impossible here.
    The three fields do not change once the record is stored, so re-reading the
    same record returns the same id.
    """
    file = str(finding.get("file") or "").strip()
    line = str(finding.get("line") or "").strip()
    text = str(finding.get("text") or "").strip()
    material = _ID_MATERIAL_SEPARATOR.join((file, line, text))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{FINDING_ID_PREFIX}{digest[:_ID_DIGEST_DIGITS]}"


def review_findings(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a review's findings, each carrying a stable id.

    A stored record already carries ``findings``; a record that carries only the
    reviewer's emitted ``raw_text`` is parsed through the shared review parser so
    the two spellings yield one list. Every finding is read through
    :func:`review.finding_claim`: an entry that states no claim is skipped,
    because an id that names no claim cannot be answered back. A bare string is
    a finding whose claim is the string and whose id is derived with the string
    as its text; a mapping takes its text from the first non-empty claim field.
    The id itself is derived exactly as :func:`finding_id` reads today, from the
    finding's ``file``, ``line`` and ``text`` key, so an id already acknowledged
    or resumed keeps resolving.

    The severity the finding states is carried through under ``severity``, and
    only then: a finding that stated none has no key here either, so the reader
    that must tell a declared non-blocking finding from an unstated severity
    can. The key's presence is the declaration, which is why it is never
    defaulted.
    """
    findings = review.get("findings")
    if not isinstance(findings, list) and review.get("raw_text"):
        parsed = review_module.parse_review(str(review["raw_text"]))
        findings = parsed.get("findings")
    if not isinstance(findings, list):
        return []
    composed: list[dict[str, Any]] = []
    for finding in findings:
        claim = review_module.finding_claim(finding)
        if claim is None:
            continue
        if isinstance(finding, Mapping):
            entry: dict[str, Any] = {
                "id": finding_id(finding),
                "file": str(finding.get("file") or "").strip(),
                "line": str(finding.get("line") or "").strip(),
                "text": claim,
            }
            severity = finding.get("severity")
            if severity is not None:
                entry["severity"] = str(severity).strip()
        else:
            # A bare string names no file or line, and its id is derived with
            # the string as the finding's text, so re-reading the record yields
            # the same id.
            entry = {
                "id": finding_id({"text": claim}),
                "file": "",
                "line": "",
                "text": claim,
            }
        composed.append(entry)
    return composed


# The two states a finding sits in for repair, decided once so the brief, the
# scope and the refusal reason can never disagree about which list a finding
# belongs to.
_BLOCKING = "blocking"
_NON_BLOCKING = "non-blocking"


def _parsed_moment(value: str) -> datetime | None:
    """Parse an ISO-8601 instant, or ``None`` when it does not name one.

    A trailing ``Z`` is read as UTC and a naive stamp is taken as UTC, so the
    store's own offset-bearing spellings and a hand-written stamp compare
    alike. An unparseable or absent value yields ``None`` rather than raising,
    because the caller decides what an undateable record means.
    """
    moment = parse_iso(str(value or ""))
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _record_predates_unmarked_audit(review: Mapping[str, Any]) -> bool:
    """Whether a record was written before unmarked findings began to be flagged.

    The review's own ``timestamp`` is compared against the moment the write-time
    severity audit landed. A record stamped before it, or one carrying no
    readable stamp at all, is treated as written before the instruction, so its
    absent severities carry no flag. This decides the flag alone: a finding that
    declares no severity is repaired either way.
    """
    recorded = _parsed_moment(str(review.get("timestamp") or ""))
    threshold = _parsed_moment(UNMARKED_SEVERITY_AUDIT_STARTED_AT)
    if recorded is None or threshold is None:
        return True
    return recorded < threshold


def _finding_kind(finding: Mapping[str, Any]) -> str:
    """Classify one finding as blocking or a non-blocking finding.

    A declared blocking severity blocks; any other declared severity is
    non-blocking, recorded on the review rather than repaired. A finding that
    declares nothing — no key, an empty value, or a word outside the vocabulary
    — blocks too: the store admits such a finding rather than refusing it, so an
    unreadable severity is a judgement the reviewer left unstated, not a
    non-blocking finding, and repairing it is what keeps a finding a reviewer
    raised from being discarded. Which values count as a declaration is
    :func:`declared_severity`'s decision, shared with the write-time audit.
    """
    severity = review_module.declared_severity(finding)
    if severity is None:
        return _BLOCKING
    if severity == review_module.BLOCKING_FINDING_SEVERITY:
        return _BLOCKING
    return _NON_BLOCKING


def _findings_of_kind(review: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    """The record's findings of one kind, in the record's order."""
    return [
        finding for finding in review_findings(review) if _finding_kind(finding) == kind
    ]


def blocking_findings(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return open blocking findings a repair answers.

    This is the one filtered list both the brief and the write scope are built
    from, so the goals, the ids the done-when names and the paths the scope
    grants can never disagree about which findings became work. A finding that
    declares no severity blocks, because the store admits it rather than
    refusing it and it is a finding a reviewer raised. A review whose findings
    are all non-blocking or disposed yields an empty list, which composes no
    repair rather than a node with an empty brief.
    """
    return [
        finding
        for _, finding, disposition in blocking_finding_dispositions(review)
        if disposition == "open"
    ]


def blocking_finding_dispositions(
    review: Mapping[str, Any],
) -> list[tuple[int, dict[str, Any], str]]:
    """Keep blocking findings paired with their original indexes and answers."""
    from reckon.crew.findings import finding_disposition, parse_finding_disposition

    return [
        (
            index,
            finding,
            parse_finding_disposition(finding_disposition(review, index))["kind"],
        )
        for index, finding in enumerate(review_findings(review))
        if _finding_kind(finding) == _BLOCKING
    ]


def unmarked_findings(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the findings that declare no severity on a post-audit record.

    These are reported, not dropped: the write-time severity audit flags such a
    finding to the reviewer but does not refuse the write, so a record stamped
    after the audit that still carries one is a record whose reviewer left the
    severity unstated. A finding declares nothing when it has no key, an empty
    value, or a word outside the vocabulary — the same decision the audit reads
    through :func:`declared_severity`, so the two never disagree about a record
    one calls unmarked and the other repairs. The finding is repaired as
    blocking while open; listing it here lets the composed node name it for the
    coordinator. A record stamped before the audit predates the instruction, so
    it carries no such flag.
    """
    if _record_predates_unmarked_audit(review):
        return []
    return [
        finding
        for _, finding, disposition in blocking_finding_dispositions(review)
        if disposition == "open" and review_module.declared_severity(finding) is None
    ]


def non_blocking_findings(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the findings that declare a non-blocking severity.

    A non-blocking finding is recorded on the review rather than repaired, and
    it is named apart from an unmarked finding so a refusal can report the two
    counts separately.
    """
    return _findings_of_kind(review, _NON_BLOCKING)


def repair_round_id(
    review: Mapping[str, Any], *, reviewed_run_id: str | None = None
) -> str:
    """Return the identity of the round a review belongs to.

    The round is the reviewed run at the head the review read: two reviews of
    one run at two heads are two rounds, and two reads of one stored record are
    one round. The head comes from the record's own revision pair, so a caller
    need not supply it separately.
    """
    run_id = str(review.get("reviewed_run_id") or reviewed_run_id or "").strip()
    _, _, _, head = review_module.carried_revision_pair(review)
    head = str(head or "").strip()
    return f"{run_id}@{head}" if head else run_id


# How many hexadecimal digits of the reviewed head a round's node id keeps. The
# composition rule and the reader that strips the suffix share it, so a reader
# parses by the grammar the writer enforced.
_ROUND_HEAD_DIGITS = 12

# The rubric suffixes a round's node id may carry after the head it read. Some
# rounds belong to a rubric other than the default — a figure review is filed
# under its own — so the id names the rubric as well as the head. The reader
# strips such a suffix only when a ``-<head[:12]>`` head suffix precedes it, so
# a source whose own name ends in a rubric word, with no head written before it,
# is read whole.
_ROUND_RUBRIC_SUFFIXES = (f"-{review_module.FIGURE_REVIEW_RUBRIC}",)


def _head_suffix_at(text: str, cut: int) -> bool:
    """Whether ``text`` carries ``-<head[:12]>`` ending at ``cut``."""
    if cut < _ROUND_HEAD_DIGITS + 1 or text[cut - (_ROUND_HEAD_DIGITS + 1)] != "-":
        return False
    return bool(
        re.fullmatch(
            rf"[0-9A-Fa-f]{{{_ROUND_HEAD_DIGITS}}}",
            text[cut - _ROUND_HEAD_DIGITS : cut],
        )
    )


def _trailing_rubric_start(text: str) -> int:
    """Where a trailing rubric suffix begins, or ``-1`` when there is none.

    A rubric suffix counts only when the head the writer prefixes, one
    separator and the writer's digit count immediately precede it, so a bare
    source that merely reads as the suffix is never mistaken for a suffixed
    round.
    """
    for suffix in _ROUND_RUBRIC_SUFFIXES:
        if not text.endswith(suffix):
            continue
        cut = len(text) - len(suffix)
        if _head_suffix_at(text, cut):
            return cut
    return -1


def round_node_id(prefix: str, source: str, head: str, *, rubric: str = "") -> str:
    """The node id a round carries: ``<prefix><source>[-<head[:12]>[-<rubric>]]``.

    A round's id carries the head it read, so two rounds of one source at
    different heads never share an id and each gets its own worktree path. The
    rule is shared by the repair and review prefixes rather than written once
    per prefix, so both cannot drift into different spellings of one identity.
    A round with no head to name — a source that predates the field — keeps the
    bare ``<prefix><source>`` spelling.

    ``rubric`` names a rubric other than the default the round belongs to, and
    is written after the head suffix so the round's own readers, which parse
    head-then-rubric, recover both the source and the head. It is written only
    beside a head, because a rubric suffix with no head before it would read as
    part of the source rather than as a rubric.
    """
    source = str(source or "").strip()
    head = str(head or "").strip()
    if head:
        node_id = f"{prefix}{source}-{head[:_ROUND_HEAD_DIGITS]}"
        suffix = str(rubric or "").strip()
        return f"{node_id}-{suffix}" if suffix else node_id
    return f"{prefix}{source}"


def round_node_source(node_id: str, prefix: str) -> str:
    """Strip ``prefix`` and a trailing head suffix from a round node id.

    The inverse of :func:`round_node_id`: a reader recovering the source a
    round was composed from removes the prefix and, when present, the
    ``-<head[:12]>`` suffix and the ``-<rubric>`` suffix that follows it. A node
    id carrying no such suffix — one written before rounds were head-keyed — is
    returned with only the prefix removed, so both spellings resolve to the same
    source, and a source whose own name ends in a rubric word is left whole.
    """
    text = str(node_id or "")
    if not text.startswith(prefix):
        return ""
    source = text[len(prefix) :]
    rubric = _trailing_rubric_start(source)
    if rubric >= 0:
        source = source[:rubric]
    cut = len(source) - (_ROUND_HEAD_DIGITS + 1)
    if cut > 0 and source[cut] == "-":
        tail = source[cut + 1 :]
        if re.fullmatch(rf"[0-9A-Fa-f]{{{_ROUND_HEAD_DIGITS}}}", tail):
            return source[:cut]
    return source


def round_node_head_suffix(node_id: str, prefix: str) -> str:
    """The head a head-keyed round node id mints, or ``""`` when it carries none.

    :func:`round_node_source` strips the ``-<head[:12]>`` suffix
    :func:`round_node_id` writes, discarding the head a round was dispatched to
    read. A caller that needs that head back — comparing it against the head a
    row recorded — reads it here rather than re-parsing the node id, so the
    grammar stays the writer's own, read back through its inverse. The suffix is
    recovered only when the id is exactly the prefix, the source, one separator
    and the writer's digit count, followed by nothing but an optional rubric
    suffix, so a hand dispatch under a coordinator's own id carries no head and
    is left unresolved rather than guessed. A rubric suffix that follows the
    head is dropped with it, so a figure review's id reads back the head its
    source was reviewed at exactly as any other round's does.
    """
    text = str(node_id or "")
    if not text.startswith(prefix):
        return ""
    raw = text[len(prefix) :]
    source = round_node_source(text, prefix)
    if not source or not raw.startswith(source + "-"):
        return ""
    tail = raw[len(source) + 1 :]
    head = tail[:_ROUND_HEAD_DIGITS]
    if not re.fullmatch(rf"[0-9A-Fa-f]{{{_ROUND_HEAD_DIGITS}}}", head):
        return ""
    remainder = tail[_ROUND_HEAD_DIGITS:]
    if remainder and remainder not in _ROUND_RUBRIC_SUFFIXES:
        return ""
    return head


def repair_node_id(
    review: Mapping[str, Any],
    *,
    reviewed_run_id: str | None = None,
    source_node: str = "",
) -> str:
    """Return the node id a repair of this round carries.

    The id is derived from the reviewed run's node id (or its run id when it has
    no node id) and the head the review read, and it is the same id each time the
    round is composed. A run reviewed again at a later head composes a different
    id, so a later round is not mistaken for another attempt of the first.
    """
    run_id = str(review.get("reviewed_run_id") or reviewed_run_id or "").strip()
    source = str(source_node or run_id).strip()
    _, _, _, head = review_module.carried_revision_pair(review)
    return round_node_id(REPAIR_NODE_PREFIX, source, str(head or "").strip())


def _run_fence(run_record: Mapping[str, Any] | None) -> list[str]:
    """The write paths the reviewed run was itself granted.

    A run's fence lives on its node record; a caller may also pass the paths
    directly. Both spellings are read so a record and a bare fence are accepted
    alike, and the order the fence declared is preserved.
    """
    if not run_record:
        return []
    node = run_record.get("node")
    declared = node.get("write_paths") if isinstance(node, Mapping) else None
    if declared is None:
        declared = run_record.get("write_paths")
    if not isinstance(declared, (list, tuple)):
        return []
    return [str(path).strip() for path in declared if str(path).strip()]


def repair_write_scope(
    review: Mapping[str, Any],
    *,
    run_record: Mapping[str, Any] | None = None,
    fence: Sequence[str] = (),
) -> list[str]:
    """Return the union of the reviewed run's fence and the finding paths.

    The fence is laid first because it is the reviewed run's own grant, and the
    paths the findings name follow it in the record's order. Duplicates collapse
    to their first occurrence so a finding naming a path the fence already
    granted does not read as two entries. A finding that names a path outside the
    fence therefore grants it here — the repair is dispatched with the scope its
    brief implies rather than refused the file it must edit. Only a finding that
    blocks contributes a path: a non-blocking finding is recorded on the review,
    not work, so it must not widen the scope the repair is fenced into.
    """
    scope: list[str] = []
    for path in [*_run_fence(run_record), *[str(item).strip() for item in fence]]:
        if path and path not in scope:
            scope.append(path)
    for finding in blocking_findings(review):
        path = finding["file"]
        if path and path not in scope:
            scope.append(path)
    return scope


def _finding_location(finding: Mapping[str, Any]) -> str:
    """Return a finding's location as ``file`` or ``file:line``.

    A finding that carries no file has no location to name, whatever line it
    states, so this returns nothing and the caller's own fallback applies. A
    line rendered without its file is not a place a reader can open, and it is
    truthy, so a caller testing the location for emptiness would take the bare
    line for a location and never reach its fallback.
    """
    location = str(finding.get("file") or "")
    if not location:
        return ""
    line = str(finding.get("line") or "")
    return f"{location}:{line}" if line else location


def compose_repair_brief(
    review: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    *,
    reviewed_run_id: str = "",
    unmarked: Sequence[Mapping[str, Any]] = (),
    diff: str = "",
    answers: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Return the brief listing every finding under its stable id.

    Each line names one finding by id, its location, and its text, so a repair
    reading the brief can answer the finding by id without re-reading the stored
    record. The closing clause states the manifest contract: a finding the
    repair declines must be answered with a one-line reason, and one it acts on
    must name the commit that answered it. A finding left neither acted nor
    declined is unanswered work, which is the state this node exists to prevent.

    A finding that declares no severity on a record written after the audit is
    repaired like any blocking finding and also listed separately by file and
    line, so the repair is told the record left its severity unstated without
    that finding being dropped from the work.
    """
    run_id = str(review.get("reviewed_run_id") or reviewed_run_id or "").strip()
    lines = [
        f"Repair every finding below on run {run_id}, in one node.",
        "",
    ]
    lines += [
        f"- [{finding['id']}] {_finding_location(finding)} {finding['text']}".rstrip()
        for finding in findings
    ]
    lines += [
        "",
        "In your manifest, answer every finding by id: one line naming the id and",
        f"the commit that answered it ('{ACTED_ACTION} <id>: <commit>'), or, for a",
        "finding you do not act on, a line naming the id and a one-line reason",
        f"('{DECLINED_ACTION} <id>: <reason>'). A finding left unanswered is not",
        "repaired.",
    ]
    if unmarked:
        lines += [
            "",
            "These findings declare no severity on a record written after the",
            "severity audit began flagging them; they are repaired above and",
            "flagged here:",
            *(f"- {_finding_location(finding)}" for finding in unmarked),
        ]
    if answers:
        lines += [
            "",
            "Answers already recorded on this review:",
            *(
                f"- [{answer.get('id', '')}] {answer.get('action', '')}"
                f"{': ' + str(answer['detail']) if answer.get('detail') else ''}"
                for answer in answers
            ),
        ]
    if diff.strip():
        lines += [
            "",
            "Changes since the last reviewed head (this round's work since the",
            "review above was stored):",
            "",
            diff.rstrip("\n"),
        ]
    return "\n".join(lines)


def compose_repair_node(
    review: Mapping[str, Any],
    *,
    run_record: Mapping[str, Any] | None = None,
    reviewed_run_id: str | None = None,
    source_node: str = "",
    fence: Sequence[str] = (),
    plan: str = "",
    section: str = "",
    session: str = "<session>",
    spec_level: str = "exact",
    time_budget: str = _DEFAULT_TIME_BUDGET,
    suite_command: str = "",
    diff: str = "",
    answers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any] | None:
    """Compose the single repair node a finding-bearing review round produces.

    Returns ``None`` when the review carries no blocking finding: a clean review
    is not work, and neither is a round whose findings are all non-blocking or
    all disposed — composing an empty node for either would dispatch a repair
    with nothing to do. When it carries blocking findings, the returned mapping
    names the node, its brief, its write scope, the findings the record left
    unmarked and the round it belongs to, so a caller can dispatch it or print
    it without re-deriving any of them. The reviewed run's node id names the
    node when the record carries one, so the repair reads as work on the run
    rather than on the review.

    ``suite_command`` is the reviewed run's own recorded suite command, when it
    had one. A non-empty command adds both suite observations to the done-when —
    the repair records a ``baseline_suite`` at its base and an ``after_suite`` at
    its head, each run with that literal command — so its own review can
    reconcile its added-failure count against the same suite the reviewed run was
    measured with. An unarmed reviewed run passes no command, and the composed
    node names neither arm.
    """
    findings = blocking_findings(review)
    if not findings:
        return None
    unmarked = unmarked_findings(review)
    run_id = str(review.get("reviewed_run_id") or reviewed_run_id or "").strip()
    project = str(review.get("project") or "").strip()
    node_id = repair_node_id(review, reviewed_run_id=run_id, source_node=source_node)
    round_id = repair_round_id(review, reviewed_run_id=run_id)
    scope = repair_write_scope(review, run_record=run_record, fence=fence)
    brief = compose_repair_brief(
        review,
        findings,
        reviewed_run_id=run_id,
        unmarked=unmarked,
        diff=diff,
        answers=answers,
    )
    ids = ", ".join(finding["id"] for finding in findings)
    done_when = (
        f"the repair for {round_id} commits a change answering each of the "
        f"{len(findings)} finding(s) ({ids}) and its manifest names every "
        f"finding by id — `{ACTED_ACTION} <id>: <commit>` for a finding it "
        f"answered and `{DECLINED_ACTION} <id>: <reason>` for one it declines "
        "— with no finding left unanswered"
    )
    if suite_command:
        done_when += (
            f", and records in its manifest both `baseline_suite`, measured at "
            f"the repair's base, and `after_suite`, measured at its head, each "
            f"run with exactly `{suite_command}`"
        )
    return {
        "node_id": node_id,
        "round_id": round_id,
        "run_id": run_id,
        "project": project,
        "plan": plan,
        "section": section,
        "session": session,
        "role": "implement",
        "spec_level": spec_level,
        "time_budget": time_budget,
        "goal": f"repair {len(findings)} finding(s) ({ids}) on run {run_id}",
        "brief": brief,
        "findings": findings,
        "unmarked_findings": unmarked,
        "write_path": scope[0] if scope else "",
        "write_paths": scope,
        "done_when": done_when,
        # The scope is composed from the findings, so it names the test files
        # the review flagged. A node that writes a check declares the mutation
        # that check must fail against, and the mutation here is the repair's own
        # change: reverting the commit that answered a finding must make the
        # check the reviewer named fail, or the check was never measuring it.
        "negative_control": (
            "revert each answering commit in turn and confirm the check the "
            "finding names fails without it"
        ),
    }


def _figure_review_of(
    review: Mapping[str, Any], *, base_dir: str | None = None
) -> Mapping[str, Any] | None:
    """The figure review stored for the same run and head as ``review``, if any.

    A run that drew a figure carries a second review at the same head, filed
    under the figure rubric rather than the review's own default rubric. It is
    read by the record's own project, reviewed run and carried head, so the two
    records this node pairs are the two a single review round stands for. A
    record that is itself a figure review names no companion, and one that names
    no project or reviewed run has nothing to key the read on, so both yield
    ``None``.
    """
    if (
        str(review.get("rubric") or "").strip().lower()
        == review_module.FIGURE_REVIEW_RUBRIC
    ):
        return None
    project = str(review.get("project") or "").strip()
    run_id = str(review.get("reviewed_run_id") or "").strip()
    if not project or not run_id:
        return None
    _, _, _, head = review_module.carried_revision_pair(review)
    try:
        return review_module.read_review(
            project,
            run_id,
            base_dir=base_dir,
            reviewed_head_sha=str(head or "").strip() or None,
            rubric=review_module.FIGURE_REVIEW_RUBRIC,
        )
    except (OSError, ValueError):
        return None


def review_with_figure(
    review: Mapping[str, Any], *, base_dir: str | None = None
) -> dict[str, Any]:
    """Return ``review`` with its run's figure review findings appended.

    The findings a repair answers are the run review's and the figure review's
    together: a run whose figure review raises a blocking finding goes back to
    the node that drew it, so the round the composer opens names the figure as
    well as the finding. The figure record is read for the run and head the text
    record names and its findings are appended after the text record's, in the
    record's order. The figure record's own per-finding dispositions are re-keyed
    to the appended positions, so a figure finding already disposed on its own
    record reads as disposed here too rather than being repaired a second time.

    The text record's identity fields are kept, because the round a repair
    belongs to is the run and head the *review* stands for. A review with no
    figure record, a figure record carrying no finding, or a record that is
    itself the figure review is returned unchanged, so a run that drew no figure
    composes exactly as it did before.
    """
    figure = _figure_review_of(review, base_dir=base_dir)
    if figure is None:
        return dict(review)
    figure_raw = figure.get("findings")
    if not isinstance(figure_raw, list) or not figure_raw:
        return dict(review)
    text_raw = review.get("findings")
    text_raw = list(text_raw) if isinstance(text_raw, list) else []
    merged = dict(review)
    merged["findings"] = [*text_raw, *figure_raw]
    offset = len(review_findings(review))
    dispositions: dict[str, Any] = {}
    existing = review.get(review_module.FINDING_DISPOSITIONS_KEY)
    if isinstance(existing, Mapping):
        dispositions.update({str(key): value for key, value in existing.items()})
    figure_dispositions = figure.get(review_module.FINDING_DISPOSITIONS_KEY)
    if isinstance(figure_dispositions, Mapping):
        for key, value in figure_dispositions.items():
            try:
                index = int(str(key))
            except (TypeError, ValueError):
                continue
            dispositions[str(index + offset)] = value
    if dispositions:
        merged[review_module.FINDING_DISPOSITIONS_KEY] = dispositions
    return merged


def compose_repair_for_run(
    project: str,
    reviewed_run_id: str,
    *,
    base_dir: str | None = None,
    reviewed_head_sha: str | None = None,
    run_record: Mapping[str, Any] | None = None,
    **node_kwargs: Any,
) -> dict[str, Any] | None:
    """Compose the repair node for a run's stored review, or ``None``.

    The record is read through the shared review store, so the finding set this
    composes from is the one another reader selects for the same run and head.
    The run's figure review at that head is folded in through
    :func:`review_with_figure`, so a blocking finding it raises opens the same
    round the text review would — a figure defect goes back to the node that
    drew it rather than waiting for a coordinator. A run with no stored review,
    or one whose reviews together carry no blocking finding, composes nothing.
    """
    review = review_module.read_review(
        project,
        reviewed_run_id,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
    )
    if review is None:
        return None
    review = review_with_figure(review, base_dir=base_dir)
    return compose_repair_node(
        review,
        run_record=run_record,
        reviewed_run_id=reviewed_run_id,
        **node_kwargs,
    )
