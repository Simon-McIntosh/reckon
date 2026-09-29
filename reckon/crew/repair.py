"""Compose the one repair node a finding-bearing review round produces.

A review that finds something has to become work, and the work is one node per
review *round* rather than one node per finding: a repair that answers three
findings in one turn costs one dispatch, where three repairs cost three lanes
and three merges against the same files. This module turns a stored review
record into that single node — its brief lists every finding under a stable id,
its write scope is the union of the paths the findings name and the reviewed
run's own fence, and its manifest contract obliges the repair to answer every
finding it declines with a reason.

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
from collections.abc import Mapping, Sequence
from typing import Any

from reckon.crew import review as review_module
from reckon.crew.plan_review import RESPONSE_ACTIONS

# The node-id prefix a composed repair carries, so a run can be recognised as
# the repair of a reviewed run without consulting the dispatch record. It sits
# beside the review prefix rather than sharing it: a repair that reviewed a
# review would otherwise be indistinguishable from a review of a review.
REPAIR_NODE_PREFIX = "repair-of-"

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
    the two spellings yield one list. A malformed entry is skipped rather than
    given an invented identity, because an id that does not survive a re-read of
    the record cannot be answered back.
    """
    findings = review.get("findings")
    if not isinstance(findings, list) and review.get("raw_text"):
        parsed = review_module.parse_review(str(review["raw_text"]))
        findings = parsed.get("findings")
    if not isinstance(findings, list):
        return []
    composed: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        composed.append(
            {
                "id": finding_id(finding),
                "file": str(finding.get("file") or "").strip(),
                "line": str(finding.get("line") or "").strip(),
                "text": str(finding.get("text") or "").strip(),
            }
        )
    return composed


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
    head = str(head or "").strip()
    if head:
        return f"{REPAIR_NODE_PREFIX}{source}-{head[:12]}"
    return f"{REPAIR_NODE_PREFIX}{source}"


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
    brief implies rather than refused the file it must edit.
    """
    scope: list[str] = []
    for path in [*_run_fence(run_record), *[str(item).strip() for item in fence]]:
        if path and path not in scope:
            scope.append(path)
    for finding in review_findings(review):
        path = finding["file"]
        if path and path not in scope:
            scope.append(path)
    return scope


def compose_repair_brief(
    review: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    *,
    reviewed_run_id: str = "",
) -> str:
    """Return the brief listing every finding under its stable id.

    Each line names one finding by id, its location, and its text, so a repair
    reading the brief can answer the finding by id without re-reading the stored
    record. The closing clause states the manifest contract: a finding the
    repair declines must be answered with a one-line reason, and one it acts on
    must name the commit that answered it. A finding left neither acted nor
    declined is unanswered work, which is the state this node exists to prevent.
    """
    run_id = str(review.get("reviewed_run_id") or reviewed_run_id or "").strip()
    lines = [
        f"Repair every finding below on run {run_id}, in one node.",
        "",
    ]
    for finding in findings:
        location = finding["file"]
        if finding["line"]:
            location = f"{location}:{finding['line']}"
        lines.append(f"- [{finding['id']}] {location} {finding['text']}".rstrip())
    lines += [
        "",
        "In your manifest, answer every finding by id: one line naming the id and",
        f"the commit that answered it ('{ACTED_ACTION} <id>: <commit>'), or, for a",
        "finding you do not act on, a line naming the id and a one-line reason",
        f"('{DECLINED_ACTION} <id>: <reason>'). A finding left unanswered is not",
        "repaired.",
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
) -> dict[str, Any] | None:
    """Compose the single repair node a finding-bearing review round produces.

    Returns ``None`` when the review carries no finding: a clean review is not
    work, and composing an empty node for it would dispatch a repair with
    nothing to do. When it carries findings, the returned mapping names the
    node, its brief, its write scope and the round it belongs to, so a caller
    can dispatch it or print it without re-deriving any of the three. The
    reviewed run's node id names the node when the record carries one, so the
    repair reads as work on the run rather than on the review.
    """
    findings = review_findings(review)
    if not findings:
        return None
    run_id = str(review.get("reviewed_run_id") or reviewed_run_id or "").strip()
    project = str(review.get("project") or "").strip()
    node_id = repair_node_id(review, reviewed_run_id=run_id, source_node=source_node)
    round_id = repair_round_id(review, reviewed_run_id=run_id)
    scope = repair_write_scope(review, run_record=run_record, fence=fence)
    brief = compose_repair_brief(review, findings, reviewed_run_id=run_id)
    ids = ", ".join(finding["id"] for finding in findings)
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
        "write_path": scope[0] if scope else "",
        "write_paths": scope,
        "done_when": (
            f"the repair for {round_id} commits a change answering each of the "
            f"{len(findings)} finding(s) ({ids}) and its manifest names every "
            f"finding by id — `{ACTED_ACTION} <id>: <commit>` for a finding it "
            f"answered and `{DECLINED_ACTION} <id>: <reason>` for one it declines "
            "— with no finding left unanswered"
        ),
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
    A run with no stored review, or one whose review carries no finding,
    composes nothing.
    """
    review = review_module.read_review(
        project,
        reviewed_run_id,
        base_dir=base_dir,
        reviewed_head_sha=reviewed_head_sha,
    )
    if review is None:
        return None
    return compose_repair_node(
        review,
        run_record=run_record,
        reviewed_run_id=reviewed_run_id,
        **node_kwargs,
    )
