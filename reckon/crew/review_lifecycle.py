"""Derive the lifecycle state of a stored review from the record and its subject.

A review is evidence about a plan version or a run, and how long it stays
interesting is a fact about what it reviewed rather than a field the record
carries. Storing the state on the record would make it a thing two writers
disagree about — a landing beat sets it on the plan while the review record is
rewritten for an answer — and a directory would mark membership by where a file
sits, which is not a fact any reader can check against the work. So the state is
derived here, once, from the record and the state of its subject, and the set of
records still worth a reader's attention is the derivation's output rather than
a place in the tree.

Four states, checked in this order, first match wins:

``landed``
    The work the review was about is in the past: a plan review whose plan has
    shipped, or every section the review read is now declared done; a run review
    whose reviewed run has a ledger row — promoted, or closed without promotion.
    A landed review is the archive — kept, and read by the look-back views, but
    no longer carrying a live obligation.
``superseded``
    A later full review of the same subject exists, so this one describes
    content that has since been re-read. A plan review is superseded by a later
    review of the same plan under the same rubric — design and content reviews
    are separate subjects — ordered by plan version and then by dispatch time; a
    run review by a later review of the same run, ordered by dispatch time.
``open``
    A blocking finding is unanswered. A plan review's blocking findings are the
    ones the dispatch gate still refuses on, read through
    :func:`reckon.crew.plan_review.unanswered_findings`; a run review's are
    every finding whose severity is not ``follow-on``.
``answered``
    None of the above: every blocking finding has an answer and no later review
    of the subject exists.

:data:`HOT_STATES` is the pair a default view reads — the reviews still owing
someone something. A derived set rather than a stored one, so a plan shipping
moves its reviews to ``landed`` while every record keeps its own bytes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon import _plan_html, _store, ledger
from reckon._plan_html import DOCUMENT_UNIT, section_record_id
from reckon.crew import plan_review as _plan_review
from reckon.crew import review as _review_store

LANDED = "landed"
SUPERSEDED = "superseded"
OPEN = "open"
ANSWERED = "answered"

# The states a review remains live in. A view that shows what still needs an
# answer reads these two and leaves the archive to the look-back views, so the
# hot set is derived here beside the states rather than restated by each reader.
HOT_STATES = frozenset({OPEN, ANSWERED})

# The plan statuses that mean the reviewed work is in the past.
LANDED_PLAN_STATUSES = frozenset({"shipped", "done", "archived"})

# The section declaration that means a section's work has landed.
DONE_DECLARATION = "done"

# The one severity that does not block. Every other declared severity, and a
# finding that declares none, blocks: a run review is asked for a judgement, and
# an unstated severity is a judgement not yet made rather than a follow-on.
NON_BLOCKING_SEVERITIES = frozenset({"follow-on"})

# The keys a record's findings may sit under. A record that carries no findings
# list has nothing to be open about, which is not the same as a blocking finding
# that happens to be answered.
_FINDINGS_KEY = "findings"


def _is_run_review(record: Mapping[str, Any]) -> bool:
    """Whether a record reviews a run rather than a plan.

    A run review names the run it read; a plan review names a plan slug and
    version. The two subjects are disjoint in the store, so the run id is the
    discriminator.
    """
    return bool(str(record.get("reviewed_run_id") or "").strip())


def _blocking(finding: Mapping[str, Any]) -> bool:
    """Whether a stored run finding blocks, by its declared severity.

    The predicate is defined once here so the derivation and any later reader
    agree about which findings a run review still owes an answer to.
    """
    severity = str(finding.get("severity") or "").strip().lower()
    return severity not in NON_BLOCKING_SEVERITIES


def _answered(record: Mapping[str, Any], finding_id: str) -> bool:
    """Whether a record carries an answer for one finding.

    An answer lives in the latest-per-finding ``responses`` map or in the
    append-only ``response_events`` list; either carries an entry for the
    finding. Presence is the answer: how the finding was answered — acted on or
    declined — is the record's business, not this derivation's.
    """
    responses = record.get("responses")
    if isinstance(responses, Mapping) and finding_id in responses:
        return True
    events = record.get("response_events")
    if isinstance(events, (list, tuple)):
        for event in events:
            if (
                isinstance(event, Mapping)
                and str(event.get("finding") or "") == finding_id
            ):
                return True
    return False


def _run_open(record: Mapping[str, Any]) -> bool:
    """Whether a run review carries a blocking finding with no answer.

    A run finding is real data without an identity: the imported store's run
    findings carry ``file``, ``line``, ``text`` and sometimes ``severity``, and
    no run record carries ``responses`` or ``response_events``. A finding
    therefore answers by identity only when it carries an id that an answer can
    name; a blocking finding with no id can be referenced by nothing, so nothing
    can answer it and it counts as unanswered. A run review whose blocking
    findings all carry ids is answered through them as before.
    """
    findings = record.get(_FINDINGS_KEY)
    if not isinstance(findings, list):
        return False
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        if not _blocking(finding):
            continue
        finding_id = str(finding.get("id") or "").strip()
        if not finding_id or not _answered(record, finding_id):
            return True
    return False


def _plan_landed(
    record: Mapping[str, Any], plan_state: Mapping[str, Any] | None
) -> bool:
    """Whether the work a plan review read has landed.

    The plan has shipped, or every section the review's own ``section_digests``
    named is now declared done. A review that named no section — one whose
    digests carry only the document unit — is not landed by the second clause,
    since nothing has collapsed for it.
    """
    if not isinstance(plan_state, Mapping):
        return False
    if str(plan_state.get("status") or "").strip().lower() in LANDED_PLAN_STATUSES:
        return True
    return _reviewed_sections_done(record, plan_state)


def _reviewed_sections_done(
    record: Mapping[str, Any], plan_state: Mapping[str, Any]
) -> bool:
    """Whether every section the review read is declared done in the plan."""
    digests = record.get("section_digests")
    declarations = plan_state.get("section_declarations")
    if not isinstance(digests, Mapping) or not isinstance(declarations, Mapping):
        return False
    done = {
        section_record_id(name)
        for name, value in declarations.items()
        if str(value).strip().lower() == DONE_DECLARATION
    }
    reviewed = {section_record_id(name) for name in digests}
    reviewed.discard(section_record_id(DOCUMENT_UNIT))
    return bool(reviewed) and reviewed <= done


def _dispatch_time(record: Mapping[str, Any]) -> str:
    """The dispatch stamp a record is ordered by, empty when it carries none.

    A committed record's dispatch time is its own ``dispatched_at``; a staging
    record stored before that key existed carries only its ``timestamp``, which
    stands in so an older record still orders against a newer one.
    """
    for key in ("dispatched_at", "timestamp"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _plan_order(record: Mapping[str, Any]) -> tuple[int, str]:
    """The (version, dispatch time) a plan review is ordered by."""
    try:
        version = int(record.get("plan_version"))
    except (TypeError, ValueError):
        version = 0
    return (version, _dispatch_time(record))


def _plan_subject(record: Mapping[str, Any]) -> tuple[str, bool]:
    """The subject a plan review is about: its plan and its rubric family."""
    return (
        str(record.get("plan_slug") or "").strip(),
        _plan_review.is_design_review(record),
    )


def _later_plan_exists(
    record: Mapping[str, Any], later_records: tuple[Mapping[str, Any], ...]
) -> bool:
    """Whether a later review of the same plan under the same rubric exists."""
    subject = _plan_subject(record)
    order = _plan_order(record)
    return any(
        _plan_subject(other) == subject and _plan_order(other) > order
        for other in later_records
    )


def _later_run_exists(
    record: Mapping[str, Any], later_records: tuple[Mapping[str, Any], ...]
) -> bool:
    """Whether a later review of the same run exists."""
    reviewed_run = str(record.get("reviewed_run_id") or "").strip()
    order = _dispatch_time(record)
    return any(
        str(other.get("reviewed_run_id") or "").strip() == reviewed_run
        and _dispatch_time(other) > order
        for other in later_records
    )


def lifecycle(
    record: Mapping[str, Any],
    *,
    plan_state: Mapping[str, Any] | None = None,
    run_closed: bool | None = None,
    later_records: Any = (),
) -> str:
    """Return the lifecycle state of one stored review.

    Pure: the state is a function of the record, its subject's state and the
    set of candidate later records, and nothing is written. ``plan_state`` is a
    parsed plan mapping and is consulted only for a plan review; ``run_closed``
    is whether the reviewed run has a ledger row — promoted, or closed without
    promotion — and is consulted only for a run review; ``later_records`` is an
    iterable of other stored records of the same kind, each a candidate for
    superseding this one.

    The states are checked in order — landed, superseded, open, answered — so a
    landed review whose finding is still unanswered reads ``landed``: the work
    has collapsed and the review's live obligation has gone with it.
    """
    candidates = tuple(other for other in later_records if isinstance(other, Mapping))
    if _is_run_review(record):
        if run_closed:
            return LANDED
        if _later_run_exists(record, candidates):
            return SUPERSEDED
        if _run_open(record):
            return OPEN
        return ANSWERED
    if _plan_landed(record, plan_state):
        return LANDED
    if _later_plan_exists(record, candidates):
        return SUPERSEDED
    if _plan_review.unanswered_findings(record):
        return OPEN
    return ANSWERED


def is_hot(
    record: Mapping[str, Any],
    *,
    plan_state: Mapping[str, Any] | None = None,
    run_closed: bool | None = None,
    later_records: Any = (),
) -> bool:
    """Whether a stored review is one a live reader still acts on.

    A reader that acts on a review's findings — the dispatch gate and the
    obligations view — acts only while the review is hot: a landed review
    describes work in the past and a superseded one has been re-read, so a
    finding on either is archive rather than a live obligation. The predicate
    wraps :func:`lifecycle` so the two states a live reader consults are named
    once, beside the derivation, rather than restated by each reader. Its
    arguments are :func:`lifecycle`'s, with the same reading.
    """
    return (
        lifecycle(
            record,
            plan_state=plan_state,
            run_closed=run_closed,
            later_records=later_records,
        )
        in HOT_STATES
    )


# ── The loader ──────────────────────────────────────────────────────────────
# A thin reader: it gathers the records from the store's own readers and pairs
# each with the state :func:`lifecycle` derives for it. It walks no store of its
# own — the plan reviews come from the plan-review store's listing, the run
# reviews from the run-review store's readers, the plan states from the plan
# parser and the promotions from the ledger.


def _read_json(path: Path) -> dict[str, Any] | None:
    """Return a record's content, or ``None`` when the file will not read."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(record) if isinstance(record, Mapping) else None


def _run_review_records(
    project: str, *, base_dir: str | Path | None, root: str | Path | None
) -> dict[str, dict[str, Any]]:
    """Return every run review of a project, keyed by the path it was read from.

    The staging store is enumerated through its own reader — the run ids it
    holds, each read back through :func:`reckon.crew.review.stored_records_for_run`
    — so a run's several rounds are all reached through the store. The committed
    run tree is listed beside it, keyed by the reviewed run rather than by the
    review run, so a closed run's review is found where a promotion put it.
    """
    found: dict[str, dict[str, Any]] = {}
    for run_id in _review_store.reviewed_run_ids(project, base_dir=base_dir):
        for path, record in _review_store.stored_records_for_run(
            project, run_id, base_dir=base_dir
        ):
            if _is_run_review(record):
                found[str(path)] = record
    committed = _review_store.committed_review_root(project, root=root)
    if committed is not None:
        run_root = committed / _review_store.COMMITTED_RUN_DIRNAME
        if run_root.is_dir():
            for run_dir in sorted(run_root.iterdir()):
                if not run_dir.is_dir():
                    continue
                for path in sorted(run_dir.glob("*.json")):
                    record = _read_json(path)
                    if record is not None and _is_run_review(record):
                        found[str(path)] = record
    return found


def _plan_state(
    project: str, slug: str, *, root: str | Path | None
) -> Mapping[str, Any] | None:
    """Return a plan's parsed state, or ``None`` when its file is not readable."""
    docs_dir = _store._docs_dir_for_project(project, root)
    if docs_dir is None:
        return None
    plan_path = docs_dir / "plans" / f"{slug}.html"
    if not plan_path.is_file():
        return None
    try:
        return _plan_html.read_state(
            plan_path.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        return None


def review_lifecycles(
    project: str, *, base_dir: str | Path | None = None, root: str | Path | None = None
) -> dict[str, str]:
    """Return each stored review's path and its derived lifecycle state.

    Plan reviews are read through :func:`reckon.crew.plan_review.list_plan_reviews`,
    run reviews through the run-review store's readers, plan states through the
    plan parser and closed runs through the ledger's run-id reader. Every record
    of one kind is offered to :func:`lifecycle` as a candidate later record for
    its siblings, so a superseding review is found without a second store walk.
    """
    plan_records = _plan_review.list_plan_reviews(project, base_dir=base_dir)
    run_records = _run_review_records(project, base_dir=base_dir, root=root)
    closed = ledger.run_ids(project, root=root)

    states: dict[str, str] = {}
    for record in plan_records:
        path = str(record.get("review_path") or "")
        if not path:
            continue
        state_plan = _plan_state(project, str(record.get("plan_slug") or ""), root=root)
        states[path] = lifecycle(
            record,
            plan_state=state_plan,
            later_records=plan_records,
        )
    for path, record in run_records.items():
        run_closed = str(record.get("reviewed_run_id") or "").strip() in closed
        states[path] = lifecycle(
            record,
            run_closed=run_closed,
            later_records=tuple(run_records.values()),
        )
    return states
