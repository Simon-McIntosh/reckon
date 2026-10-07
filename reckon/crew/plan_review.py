"""Record, store and query the advisory reviews of plan versions.

A plan is reviewed before it is built: every authored change to a plan earns one
rubber-duck review of the settled version, and before an implementation node is
released the plan must carry a review of its current content in which every
finding has been answered. This module owns that record — its shape, where it
lives, how it is keyed to the plan content it read, how an author answers a
finding, and how a finding declined across plans is counted.

The record is joined to plan content by a fingerprint rather than by the plan's
version integer, and the reason is measured. ``reckon._plan_html.write_state``
and the MCP store bump ``version`` on *every* write, including the impl-only edit
that must not produce a review. A review keyed to the integer would be orphaned
by the first impl bump: the version would move, the stored review would describe
a version that no longer exists, and the dispatch gate would refuse every later
build with no review able to clear it. So the fingerprint normalises out exactly
the server-managed metadata scalars — the plan's version, modified stamp,
implementation fraction, status, ROI, effort, owner, sprint, tags and archive
flag — together with the ``commits`` the landing beat records — and digests the
authored content: the sections' prose and the document
unit's decisions, gates, dependencies and other authored state. Comments and
followups are normalised out beside the metadata scalars, both the authored and
the promotion's landing ones, because they record work done and next steps
rather than the design a review reads; the section declarations are normalised
out too, since a landing collapse rewrites a section's lifecycle declaration in
the same write that replaces its body with a landed card. The parser derives a little
more state from those scalars, so :data:`PLAN_DERIVED_SCALARS` removes that
too; without it, adding the named effort field to a plan that carried only the
legacy effort letter would move the fingerprint through the derived calibration
flag and orphan a review on exactly the plans that predate the named field. A
metadata-only write therefore neither triggers a review nor invalidates one,
and an authored edit changes the fingerprint so the gate demands a fresh
review.

The store reuses the crew review store root, so a plan review is queryable
across plans and projects beside the code reviews: ``reviews/<project>/
plan-<slug>.v<N>.json``, with a ``.at-<blob8>`` sibling when one version is
reviewed twice. The second review of a version lands beside the first rather
than over it, because a re-review accumulates as evidence next to the review
that motivated it. ``reviewed_blob_sha`` is the git blob sha of the reviewed
bytes: content-addressing is what lets a review taken against an uncommitted
MCP write join the committed plan later, since identical bytes hash identically
in a worktree, the main checkout and a commit.

A finding's answer is an act or a decline with a one-line reason each. The
decline's reason is the record, not decoration: a repeated decline of one
finding type across three or more distinct plans means either the rubric is
wrong or a practice is, and either way the lead rules on it — so a reasonless
decline is refused rather than stored, and :func:`declined_recurrence` counts
distinct plans per finding type so that recurrence can reach the lead.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shlex
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _plan_html
from reckon._plan_html import DOCUMENT_UNIT, landed_section_ids
from reckon._store import write_json_atomically
from reckon.crew import review as _review_store

# Reviewer-text parsing is owned by reckon.crew.review, so the code-review and
# plan-report grammars share one line reader rather than each defining their
# own. This module keeps only the store-side logic; the parser is re-exported
# under the name its callers already use.
parse_review_report = _review_store.parse_plan_review_report

# ── The fingerprint exclusion set ───────────────────────────────────────────
# These are the bookkeeping scalars: written by the store, the web surface or
# the landing beat, never authored as plan content. A change to any of them
# neither requires a review nor invalidates one, so they are normalised out of
# the digest. ``commits`` belongs here although the landing beat writes it rather
# than the store: it records which commits landed the plan, which is provenance
# rather than the design a review reads, and the beat sets it on every landing.
# Left in, each landing would move the ``_document`` digest and make the next
# dispatch refuse with ``plan-review-missing``. Every other key of the parsed
# plan is authored content and stays in.
PLAN_METADATA_SCALARS: tuple[str, ...] = (
    "version",
    "modified",
    "impl",
    "status",
    "roi",
    "effort_hours",
    "owner",
    "sprint",
    "tags",
    "archived",
    "commits",
)

# ── The derived-field exclusions ────────────────────────────────────────────
# Excluding a metadata scalar is not sufficient: the parser derives further
# state from those scalars, and a derived key left in the digest re-introduces
# the very write that must not invalidate a review. Measured by sweeping every
# plan under docs/plans and every evidence doc and editing each scalar in
# PLAN_METADATA_SCALARS in turn: exactly these keys move, and nothing else does.

#   effort_calibrated     - set from whether the plan parses an explicit
#                           ``plan-effort-hours`` meta or falls back to a legacy
#                           effort letter. It states how the effort field was
#                           supplied, not authored content, so it moves
#                           whenever ``effort_hours`` is written.
#   compatibility_warnings - the parser's diagnostics channel, whose text
#                           quotes the values it read ("legacy letter 'M' maps
#                           to 8.0 worker-hours", "wall-clock ... exceeds ...
#                           worker-hours"). It is commentary on the metadata
#                           rather than plan content; a warning about authored
#                           content sits beside the key it concerns, which the
#                           digest carries on its own.
#   impl_source           - "computed" or "authored", set from whether the
#                           parser derived ``impl`` from the section records or
#                           read it from the ``plan-impl`` meta. It states how
#                           the implementation fraction was supplied, not
#                           authored content, and every plan's first landing
#                           beat sets an impl on a plan that carried none while
#                           a later write may flip it between authored and
#                           computed — either would move the digest without an
#                           authored change.
PLAN_DERIVED_SCALARS: tuple[str, ...] = (
    "effort_calibrated",
    "compatibility_warnings",
    "impl_source",
)

# Content and design reviews share their rubric vocabulary with the report
# parser in reckon.crew.review, which owns the names and item sets.

# ── The two review moments ─────────────────────────────────────────────────
# A content review is the rubber-duck read an author composes once a writing
# session ends; a design review is the prior-art and depth read a plan carries
# before its first implementation node. Spelled as the ``review-plan`` verb
# takes them.
CONTENT_RUBRIC = "content"
DESIGN_RUBRIC = "design"

# Dispatching a review needs the coordinator session its completion is
# delivered to, which no composing surface knows, so a composed command names
# this placeholder for the reader to fill in rather than leaving the flag out.
SESSION_PLACEHOLDER = "<session>"

# A stored record spells the design rubric as the verb's flag or as the item-set
# name the report parser resolves the flag to, one of review.PLAN_REVIEW_RUBRICS.
_DESIGN_RUBRIC_NAMES = (DESIGN_RUBRIC, "plan_design_review")


def review_invocation(
    project: str,
    plan_slug: str,
    *,
    rubric: str = CONTENT_RUBRIC,
    session: str = "",
) -> str:
    """The command that composes a review of a plan, as every surface prints it.

    One composer, so a write's ``review_owed`` note, the stale-review hint and
    the dispatch gate's refusals hand their reader the same command, and one the
    verb accepts once the session is filled in.
    """
    named = shlex.quote(session) if session else SESSION_PLACEHOLDER
    return (
        f"reckon crew review-plan --project {shlex.quote(project)} "
        f"--plan {shlex.quote(plan_slug)} --rubric {rubric} --session {named} --local"
    )


def answer_invocation(project: str, plan_slug: str, finding_id: str) -> str:
    """The command that answers one finding, acted on or declined with a reason."""
    return (
        f"reckon crew review-plan --project {shlex.quote(project)} "
        f"--plan {shlex.quote(plan_slug)} --answer {shlex.quote(finding_id)} "
        "--acted (or --declined '<reason>')"
    )


# ── The promotion-comment exclusion ─────────────────────────────────────────
# A promotion appends one comment per promoted run to the section the run
# landed against, under an id derived from the run id and beginning with this
# prefix. The comment is the plan's own record that work landed, written by
# machinery rather than authored, so it is normalised out of the digest beside
# the metadata scalars: without that, every promotion moves a reviewed plan's
# fingerprint and buys the plan another review of content nobody changed.
RUN_COMMENT_PREFIX = "c-run-"


def _is_run_comment(comment_id: Any) -> bool:
    """Recognise the run-derived identity promotion uses for landing comments."""
    return str(comment_id or "")[: len(RUN_COMMENT_PREFIX)] == RUN_COMMENT_PREFIX


# The states the surface renders beside the version a review read. ``ready`` is
# what a freshly stored review carries; ``acted`` and ``declined`` are reached
# once every finding is answered; ``pending`` is the surface's state for a
# version with no stored review yet.
PLAN_REVIEW_STATUSES: tuple[str, ...] = ("pending", "ready", "acted", "declined")
DEFAULT_STATUS = "ready"

# An author answers each finding by acting on it or by declining it. Either
# answer closes the finding for the dispatch gate; a decline must carry a reason.
RESPONSE_ACTIONS: tuple[str, ...] = ("acted", "declined")

# The append-only list of every answer a record has received. The ``responses``
# map keeps only the latest answer per finding, so a finding answered twice
# keeps only the second there; this list is where the first survives, as the
# event it was. The record body itself is never rewritten.
RESPONSE_EVENTS_KEY = "response_events"

# A finding type declined across this many distinct plans surfaces to the lead.
RECURRENCE_THRESHOLD = 3


def _as_state(plan: Mapping[str, Any] | str | Path) -> Mapping[str, Any]:
    """Coerce the accepted plan inputs to parsed plan state.

    A mapping is taken as already-parsed state; a path is read and parsed as its
    document, so a path and that document's text give one fingerprint rather than
    two.
    """
    if isinstance(plan, Mapping):
        return plan
    if isinstance(plan, Path):
        return _plan_html.read_state(plan.read_text(encoding="utf-8", errors="replace"))
    if isinstance(plan, str):
        return _plan_html.read_state(plan)
    raise TypeError(
        f"plan_fingerprint expects a mapping, path or html, got {type(plan)!r}"
    )


def _as_document(plan: Mapping[str, Any] | str | Path) -> str | None:
    """Return the plan's document text, or ``None`` for an already-parsed mapping."""
    if isinstance(plan, Path):
        return plan.read_text(encoding="utf-8", errors="replace")
    if isinstance(plan, str):
        return plan
    if isinstance(plan, Mapping):
        return None
    raise TypeError(
        f"plan_fingerprint expects a mapping, path or html, got {type(plan)!r}"
    )


def _without_comments_and_followups(state: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return parsed state with authored comments and followups removed.

    The document unit digests the design a worker builds from. Comments and
    followups record work done and next steps — the landing comments and
    followups a coordinator writes as it works — not that design, so folding
    them in re-arms a review of the whole plan on every landing beat. Both keys
    are dropped entirely, so a promotion's run comments fall out with the
    authored ones. Decisions and dependencies stay: a resolved decision or a
    new dependency changes what is built, so it belongs to the reviewed content.

    A state without either key is returned with what it carries; the
    normalisation drops whole keys and never reaches inside a value it cannot
    classify.
    """
    return {
        key: value
        for key, value in state.items()
        if key not in ("comments", "followups")
    }


def _without_section_declarations(state: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return parsed state with each section's lifecycle declaration removed.

    A section carries a declaration (``deferred``, ``implementable``, ``done``)
    that a landing work-beat both reads and writes: :func:`reckon._store.
    _apply_collapse_section` sets it to ``done`` in the same write that replaces
    the section's authored body with its landed card. That declaration is
    per-section lifecycle state, the analogue of the plan-level ``status`` scalar
    already normalised out, and it is not part of the design a review reads. Left
    in, it moves the fingerprint on a landing collapse even after the card is
    excluded, so it is dropped — from the ``section_declarations`` map and from
    each ``sections`` record's ``status`` — and the section is digested by its
    identity and its structural record instead.

    A state whose keys are the wrong shape is returned with only what can be
    recognised removed: the normalisation never hides content it cannot classify.
    """
    cleaned = {
        key: value for key, value in state.items() if key != "section_declarations"
    }
    sections = cleaned.get("sections")
    if isinstance(sections, list):
        cleaned["sections"] = [
            {k: v for k, v in record.items() if k != "status"}
            if isinstance(record, Mapping)
            else record
            for record in sections
        ]
    return cleaned


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _digest_state(plan, *, keep_declarations=False):
    excluded = frozenset(PLAN_METADATA_SCALARS) | frozenset(PLAN_DERIVED_SCALARS)
    state = {
        str(key): value
        for key, value in _without_comments_and_followups(_as_state(plan)).items()
        if key not in excluded
    }
    return state if keep_declarations else _without_section_declarations(state)


def _prose_texts(plan: Mapping[str, Any] | str | Path) -> dict[str, str]:
    """Return each authored unit's prose, whitespace-normalised, in one read.

    This is the one reader the unit digests and the change measure share: a
    section's prose or the remaining document prose, taken through the one
    section-prose reader, so the measure extracts no text of its own and both
    texts it compares come from here. A parsed-state input carries no document
    text, so it yields no prose rather than an invented one.
    """
    document = _as_document(plan)
    buckets: dict[str, list[str]] = {DOCUMENT_UNIT: []}
    if document is not None:
        for identity, prose in _plan_html.section_prose(document):
            buckets.setdefault(identity, []).append(prose)
    return {
        identity: " ".join(" ".join(parts).split())
        for identity, parts in buckets.items()
    }


def _section_digests(plan: Mapping[str, Any] | str | Path) -> dict[str, str]:
    """Digest authored sections and the remaining document independently."""
    payloads = {
        identity: {"prose": prose} for identity, prose in _prose_texts(plan).items()
    }
    payloads[DOCUMENT_UNIT]["state"] = _digest_state(plan)
    return {identity: _digest(payload) for identity, payload in payloads.items()}


def plan_fingerprint(plan: Mapping[str, Any] | str | Path) -> str:
    """Return the identity of the sorted authored-unit digest mapping.

    Section digests let a review continue covering outstanding authored work
    after another section lands. Metadata and lifecycle declarations are
    normalised out, and so are comments and followups — the authored ones and
    the landing ones a promotion writes alike. Decisions, gates, dependencies
    and the other remaining authored state belong to the document unit.
    """
    return _digest(_section_digests(plan))


# The store's plan-file grammar: a plan review is stored at
# ``plan-<slug>.v<N>.json`` (with a ``.at-<blob8>`` sibling when one version is
# reviewed twice). The walk that enumerates the store and any count that
# cross-checks it must name the same pattern, so it is spelled here once and
# read by the reader and its callers rather than restated as a literal.
PLAN_REVIEW_FILE_GLOB = "plan-*.v*.json"


def plan_review_path(
    project: str,
    plan_slug: str,
    plan_version: int,
    base_dir: str | Path | None = None,
    *,
    reviewed_blob_sha: str | None = None,
    committed_root: str | Path | None = None,
    review_run_id: str | None = None,
) -> Path:
    """Return the durable path of a plan review, keyed by version and blob.

    ``reviews/<project>/plan-<slug>.v<N>.json`` is the primary path. A named
    ``reviewed_blob_sha`` selects the ``.at-<blob8>`` sibling the second review
    of one version lands on, so a re-review never overwrites its predecessor. An
    invalid blob naming is refused rather than silently normalised into a path.

    ``committed_root`` selects the committed tree instead:
    ``<committed_root>/plan/<plan-slug>/<review-id>.json``, the plan directory
    named for the reviewed plan and the file named for the review run that
    produced it (``review_run_id``). The committed file is keyed by the review
    run rather than by version or blob — a re-review is a new review run and the
    record it carries names the version it read. A committed path carrying no
    review run id is refused rather than given a name it was never told.
    """
    if committed_root is not None:
        name = str(review_run_id or "").strip()
        if not name:
            raise ValueError(
                "a committed plan review path needs the review run id that names it"
            )
        return (
            Path(committed_root)
            / _review_store.COMMITTED_PLAN_DIRNAME
            / plan_slug
            / f"{name}.json"
        )
    suffix = (
        ""
        if reviewed_blob_sha is None
        else _review_store._blob_suffix(str(reviewed_blob_sha), length=8)
    )
    version = int(plan_version)
    return (
        _review_store.review_store_root(base_dir)
        / project
        / f"plan-{plan_slug}.v{version}{suffix}.json"
    )


def _record_blob8(record: Any) -> str | None:
    """Return the first eight hex characters of a record's reviewed blob sha."""
    if not isinstance(record, Mapping):
        return None
    blob = str(record.get("reviewed_blob_sha") or "").strip()
    return blob.lower()[:8] or None


def _stored_blob8(path: Path) -> str | None:
    """Return the blob8 a stored file holds, or ``None`` if it is unreadable."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _record_blob8(stored)


def _target_path(
    project: str,
    plan_slug: str,
    plan_version: int,
    record: Mapping[str, Any],
    base_dir: str | Path | None,
) -> Path:
    """Return where a record lands: the plain path or a blob-keyed sibling.

    The first review of a version takes the plain path. Re-storing the same
    content there is idempotent, so a duplicate write overwrites its own bytes
    and no `.at-` sibling is minted for a review that changed nothing. A second
    review of the same version carrying different content moves to the
    ``.at-<blob8>`` sibling, so both files survive and the earlier review is not
    displaced by the later one.
    """
    blob8 = _record_blob8(record)
    plain = plan_review_path(project, plan_slug, plan_version, base_dir)
    if blob8 is None:
        return plain
    if not plain.is_file():
        return plain
    if _stored_blob8(plain) == blob8:
        return plain
    return plan_review_path(
        project, plan_slug, plan_version, base_dir, reviewed_blob_sha=blob8
    )


def store_plan_review(
    record: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """Persist one plan review and return the path it was written to.

    The record must name ``project``, ``plan_slug`` and ``plan_version``, which
    key the file. A missing ``timestamp`` is stamped with the current UTC moment
    so every stored record carries one; an existing timestamp is preserved. The
    ``plan_version`` is normalised to an integer and a missing status to
    :data:`DEFAULT_STATUS` on the stored copy. The write is atomic: it goes
    through the shared writer's temporary-and-rename, so a reader never sees a
    half-written record.
    """
    project = str(record.get("project") or "").strip()
    plan_slug = str(record.get("plan_slug") or "").strip()
    plan_version = record.get("plan_version")
    if not project:
        raise ValueError("plan review record is missing project")
    if not plan_slug:
        raise ValueError("plan review record is missing plan_slug")
    if not _review_store.names_a_review_subject(record):
        raise ValueError(
            "plan review record names no plan subject: a plan slug with a "
            "usable integer version is required"
        )
    if not _review_store.carries_review_material(record):
        raise ValueError(
            "plan review record carries no review material — findings, scores, "
            "a rubric or a reviewed revision — so it is not a review"
        )
    stored = dict(record)
    stored["project"] = project
    stored["plan_slug"] = plan_slug
    stored["plan_version"] = int(plan_version)
    if not stored.get("status"):
        stored["status"] = DEFAULT_STATUS
    if not stored.get("timestamp"):
        stored["timestamp"] = datetime.now(UTC).isoformat()
    path = _target_path(project, plan_slug, int(plan_version), stored, base_dir)
    write_json_atomically(
        path, stored, indent=2, sort_keys=True, fsync=False, mode=None
    )
    return path


def _candidate_paths(
    project: str,
    plan_slug: str,
    plan_version: int | None,
    base_dir: str | Path | None,
) -> list[Path]:
    """Return the plain and blob-keyed files a plan review could occupy.

    A named version restricts the search to that version's staging files; an
    unnamed version spans every version of the plan, so a caller asking "is this
    plan reviewed at this fingerprint?" searches across versions rather than
    guessing which one the stored review named.

    The project's committed plan-review tree is searched beside the staging
    root, so a review promoted out of staging is still found. A committed file
    is keyed by the review run that produced it rather than by version or blob,
    so it is enumerated by plan slug alone; the version filter is applied to the
    record's own ``plan_version`` in :func:`read_plan_review`, where the record
    content is in hand.
    """
    found: dict[str, Path] = {}
    root = _review_store.review_store_root(base_dir) / project
    if root.is_dir():
        if plan_version is not None:
            patterns = [f"plan-{plan_slug}.v{int(plan_version)}.json"]
            patterns.append(f"plan-{plan_slug}.v{int(plan_version)}.at-*.json")
        else:
            patterns = [f"plan-{plan_slug}.v*.json"]
        for pattern in patterns:
            for path in root.glob(pattern):
                found[str(path)] = path
    committed = _review_store.committed_review_root(project)
    if committed is not None:
        plan_dir = committed / _review_store.COMMITTED_PLAN_DIRNAME / plan_slug
        if plan_dir.is_dir():
            for path in sorted(plan_dir.glob("*.json")):
                found[str(path)] = path
    return sorted(found.values())


def _wanted_fingerprints(value: str | Iterable[str] | None) -> frozenset[str] | None:
    """The set of fingerprints a filter accepts, or ``None`` when unfiltered.

    A plan is joined to its review by one fingerprint, but a change to the
    fingerprint's definition means the content of a plan can carry two digests
    at once: the current one and the legacy one a review may have been stored
    under. A caller passes either a single digest or the handful that select the
    review of the content it is asking about, so a stored review equal to the
    legacy digest still counts as current and no plan is reviewed solely because
    the definition moved.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(str(item) for item in value)


def _fingerprint_forms(plan: Mapping[str, Any] | str | Path) -> tuple[str, str, str]:
    """Section-set, pre-collapse and whole-document digests of one snapshot."""
    document = _as_document(plan)
    content = document if document is not None else plan
    forms = [plan_fingerprint(content)]
    for keep_declarations in (True, False):
        payload = {"state": _digest_state(content, keep_declarations=keep_declarations)}
        if document is not None:
            keep_cards = keep_declarations or not bool(landed_section_ids(document))
            payload["prose"] = " ".join(
                prose
                for _, prose in _plan_html.section_prose(
                    document, keep_landed_cards=keep_cards
                )
                if prose
            )
        forms.append(_digest(payload))
    return tuple(forms)


def _snapshot_evidence(
    project: str, plan_slug: str, record: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, str], Mapping[str, Any] | None] | None:
    """Recompute a stored review's snapshot evidence, or ``None`` without one.

    The stored record names the run that composed the review; that run's report
    directory holds the ``plan.html`` snapshot the reviewer read. Recomputing
    the unit digests and prose from that snapshot under the present definition
    is what makes a definition change re-review nothing — the review still
    covers the content it read, measured the way the definition measures it now
    — and the snapshot's own declarations let the predicate ask whether a
    section was implementable when the review read it. ``None`` means the record
    names no run or the snapshot is missing, so the caller falls back to the
    stored digests and reads the threshold as zero for that review alone.
    """
    run_id = str(record.get("review_run_id") or "").strip()
    if not run_id:
        return None
    snapshot = (
        review_report_directory(project, plan_slug, run_id) / _REVIEW_SNAPSHOT_NAME
    )
    if not snapshot.is_file():
        return None
    text = snapshot.read_text(encoding="utf-8", errors="replace")
    return _section_digests(text), _prose_texts(text), _snapshot_declarations(text)


def _snapshot_declarations(document: str) -> Mapping[str, Any] | None:
    """Return a snapshot document's own section declarations, if it carries any."""
    try:
        state = _plan_html.read_state(document)
    except Exception:  # noqa: BLE001 - an unreadable snapshot yields no evidence
        return None
    declarations = state.get("section_declarations")
    return declarations if isinstance(declarations, Mapping) else None


# The section's closing done-when paragraph, located within its prose by this
# one locator. A done-when lives in section prose where a one-word edit falls
# far under any fraction, so the two texts are compared directly, whitespace
# already normalised by :func:`_prose_texts`, rather than measured.
_DONE_WHEN_MARKER = re.compile(r"\bdone when\b", re.IGNORECASE)


def _done_when_paragraph(prose: str) -> str | None:
    """Return the closing done-when text of a unit's prose, or ``None``."""
    normalised = " ".join(prose.split())
    match = _DONE_WHEN_MARKER.search(normalised)
    if match is None:
        return None
    return normalised[match.start() :]


def _edit_share(reviewed: str, present: str) -> float:
    """The share of the larger text's words that differ between two prose texts.

    The words are those the package's one word tokeniser yields, the same one
    the done-when overlap warning reads, so the measure adds a comparison and
    no second tokeniser. The result is the longest-common-subsequence changed
    word count over the larger word count, so a section that replaces a third
    of its words reads as a third changed against the review that read it.
    """
    from reckon.crew.dispatch import _normalised_words

    left, _ = _normalised_words(reviewed)
    right, _ = _normalised_words(present)
    larger = max(len(left), len(right))
    if larger == 0:
        return 0.0
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    matching = sum(block.size for block in matcher.get_matching_blocks())
    return (larger - matching) / larger


def review_coverage(
    project: str,
    plan_slug: str,
    *,
    plan: Mapping[str, Any] | str | Path,
    base_dir: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], set[str], dict[str, float | None]]:
    """Return contributing reviews, uncovered units and their measured change.

    A unit is covered by a stored review when it existed in that review and its
    authored prose still matches: exactly, through the digest recomputed from
    the review's snapshot, or within the change threshold, when the review's
    snapshot carries the prose to measure; a review without a snapshot has only
    its stored digests, so the threshold is read as zero for it. A unit absent
    from the review is never covered, a section the review's snapshot did not
    declare implementable counts as new, and a changed done-when leaves its
    section uncovered whatever the fraction — those three revisions are material
    beside the threshold.

    The threshold comes from the project's own resolved flight config when the
    caller passes none, so a host or project layer that retunes it changes every
    caller's verdict rather than only a caller handed a config.
    """
    from reckon import flight
    from reckon.roadmap import implementable_sections

    if config is None:
        config = flight.resolve(project=project).config
    threshold = flight.plan_review_change_threshold(config)

    document = _as_document(plan)
    content = document if document is not None else plan
    records = [
        record
        for record in list_plan_reviews(project, base_dir=base_dir)
        if record.get("plan_slug") == plan_slug
    ]
    forms = _fingerprint_forms(content)
    whole = [record for record in records if record.get("plan_fingerprint") in forms]
    outstanding = {
        _plan_html.section_record_id(section)
        for section in implementable_sections(
            _as_state(content).get("section_declarations")
        )
    } | {DOCUMENT_UNIT}
    if whole:
        # The whole-plan fingerprint is unchanged, so every unit is covered
        # except a section that became implementable since the review: a
        # declaration is normalised out of the fingerprint, so a change of
        # declaration alone reaches here and must still be caught.
        newest = max(whole, key=lambda record: int(record.get("plan_version") or 0))
        evidence = _snapshot_evidence(project, plan_slug, newest)
        snapshot_declarations = None if evidence is None else evidence[2]
        if snapshot_declarations is None:
            return [newest], set(), {}
        implemented = {
            _plan_html.section_record_id(section)
            for section in implementable_sections(snapshot_declarations)
        }
        became_implementable = {
            identity
            for identity in outstanding
            if identity != DOCUMENT_UNIT and identity not in implemented
        }
        changes = dict.fromkeys(became_implementable)
        return [newest], became_implementable, changes
    digests = _section_digests(content)
    present_prose = _prose_texts(content)
    covered: set[str] = set()
    contributing = []
    newest_evidence: dict[str, float | None] = {}
    for record in sorted(records, key=lambda item: int(item.get("plan_version") or 0)):
        evidence = _snapshot_evidence(project, plan_slug, record)
        if evidence is None:
            snapshot_digests, snapshot_prose, snapshot_declarations = None, None, None
        else:
            snapshot_digests, snapshot_prose, snapshot_declarations = evidence
        record_digests = (
            snapshot_digests
            if snapshot_digests is not None
            else record.get("section_digests")
        )
        if not isinstance(record_digests, Mapping):
            continue
        implemented = (
            None
            if snapshot_declarations is None
            else {
                _plan_html.section_record_id(section)
                for section in implementable_sections(snapshot_declarations)
            }
        )
        matched = False
        for identity in outstanding:
            if identity not in digests:
                continue
            if identity == DOCUMENT_UNIT:
                # The document unit carries decisions, gates and dependencies
                # in its digest, which are material whatever their size, so an
                # exact digest match is its only cover.
                if record_digests.get(identity) == digests[identity]:
                    covered.add(identity)
                    matched = True
                continue
            if implemented is not None and identity not in implemented:
                # The review's snapshot did not declare this section
                # implementable — absent, deferred or done there — so the
                # section is new to this review and it never covers it.
                continue
            if record_digests.get(identity) == digests[identity]:
                covered.add(identity)
                matched = True
                continue
            if snapshot_prose is None:
                continue
            reviewed_text = snapshot_prose.get(identity, "")
            present_text = present_prose.get(identity, "")
            if _done_when_paragraph(reviewed_text) != _done_when_paragraph(
                present_text
            ):
                continue
            share = _edit_share(reviewed_text, present_text)
            newest_evidence[identity] = share
            if share < threshold:
                covered.add(identity)
                matched = True
        if matched:
            contributing.append(record)
    uncovered = outstanding - covered
    changes = {identity: newest_evidence.get(identity) for identity in uncovered}
    return contributing, uncovered, changes


def _load(path: Path) -> dict[str, Any] | None:
    """Return a stored record, or ``None`` when the file is missing or unreadable."""
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(record) if isinstance(record, Mapping) else None


def read_plan_review(
    project: str,
    plan_slug: str,
    plan_version: int | None = None,
    *,
    base_dir: str | Path | None = None,
    plan_fingerprint: str | Iterable[str] | None = None,
    plan: Mapping[str, Any] | str | Path | None = None,
    reviewed_blob_sha: str | None = None,
) -> dict[str, Any] | None:
    """Return the newest stored plan review matching the given filters.

    ``plan_version`` restricts the search to one version when named. A
    ``plan_fingerprint`` filter selects the review of the content being asked
    about, which is how the caller joins a review to the plan it will build:
    the fingerprint, not the version integer, is the key that survives an impl
    bump. A ``reviewed_blob_sha`` filter selects a named content revision.
    Among the records that pass the filters the newest by file mtime is
    returned; ``None`` means no stored review matches.
    ``plan`` accepts a path, document or state and selects the review of the
    content being asked about by the coverage predicate rather than by the whole
    document, so a plan covered section by section reads as reviewed here as the
    gate reads it there; with ``plan`` given, the newest among the covering
    records — the one whose findings a caller checks — is returned, and
    ``plan_fingerprint`` is ignored.
    """
    if plan is not None:
        contributing = review_coverage(
            project, plan_slug, plan=plan, base_dir=base_dir
        )[0]
        covering = [
            record
            for record in contributing
            if (
                plan_version is None
                or int(record.get("plan_version") or 0) == int(plan_version)
            )
            and (
                reviewed_blob_sha is None
                or _review_blob_matches(record, reviewed_blob_sha)
            )
        ]
        if not covering:
            return None
        return max(covering, key=lambda record: int(record.get("plan_version") or 0))
    records: list[tuple[Path, dict[str, Any]]] = []
    wanted = _wanted_fingerprints(plan_fingerprint)
    for path in _candidate_paths(project, plan_slug, plan_version, base_dir):
        record = _load(path)
        if record is None:
            continue
        # A committed file is keyed by review run rather than by version, so a
        # named version is enforced against the record's own ``plan_version``
        # here; the staging filename already restricts a named version.
        if (
            plan_version is not None
            and record.get("plan_version") is not None
            and int(record.get("plan_version") or 0) != int(plan_version)
        ):
            continue
        if (
            wanted is not None
            and str(record.get("plan_fingerprint") or "") not in wanted
        ):
            continue
        if reviewed_blob_sha is not None and not _review_blob_matches(
            record, reviewed_blob_sha
        ):
            continue
        records.append((path, record))
    if not records:
        return None
    return max(records, key=lambda item: item[0].stat().st_mtime_ns)[1]


def _review_blob_matches(record: Mapping[str, Any], reviewed_blob_sha: str) -> bool:
    """Whether a stored review carries the named content revision, by prefix."""
    named = str(reviewed_blob_sha).strip().lower()
    carried = _record_blob8(record) or ""
    return bool(carried) and (carried.startswith(named) or named.startswith(carried))


def _plan_review_identity(record: Mapping[str, Any]) -> str:
    """Return the identity that marks one review present in both trees.

    A review is the same record whether it is read from the staging store, where
    it is keyed by plan version and blob, or from the committed tree, where it
    is keyed by the review run that produced it. The review run id names it
    across both trees, and the plan slug, version and reviewed blob distinguish
    two reviews that share a run id — a hand-written store may reuse one — so a
    listing counts a promoted review once rather than once per tree. A record
    naming no review run cannot be known to be the same as another and returns
    an empty identity, which the caller reads as "do not deduplicate".
    """
    run_id = str(record.get("review_run_id") or "").strip()
    if not run_id:
        return ""
    return "|".join(
        (
            run_id,
            str(record.get("plan_slug") or ""),
            str(record.get("plan_version") or ""),
            str(record.get("reviewed_blob_sha") or ""),
        )
    )


def list_plan_reviews(
    project: str | None = None,
    *,
    base_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return every stored plan review, newest last.

    Code reviews share the store root with plan reviews; only files named
    ``plan-<slug>.v<N>.json`` (and their ``.at-`` siblings) are staging plan
    reviews, so a listing never mixes the two populations. The project's
    committed plan-review tree is listed beside the staging root, so a review
    promoted out of staging is still found. ``project`` restricts the walk to
    one project directory; omitted, every project directory under the root is
    read. Each returned record carries its ``review_path`` so a caller can
    rewrite the very file it read.

    A record present in both trees is listed once, as the committed copy: the
    committed tree is read first and the staging enumeration skips a review run
    id the committed tree already supplied. Records are identified by the review
    run that produced them, the one key the two trees share — a staging file is
    named for the plan version and blob, a committed file for the review run.

    A project with no staging directory is still listed when it is mounted: its
    reviews may all be committed, so the staging root no longer names it even
    though its committed tree holds every one. The mounted projects are
    enumerated beside the staging directories for that reason.
    """
    from reckon import flight

    root = _review_store.review_store_root(base_dir)
    if project is not None:
        directories = [root / project]
    else:
        names: set[str] = set()
        if root.is_dir():
            names.update(entry.name for entry in root.iterdir() if entry.is_dir())
        names.update(flight.mounted_project_docs())
        directories = sorted(root / name for name in names)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _collect(path: Path, default_project: str) -> None:
        record = _load(path)
        if record is None:
            return
        identity = _plan_review_identity(record)
        if identity and identity in seen:
            return
        if identity:
            seen.add(identity)
        record.setdefault("project", default_project)
        record["review_path"] = str(path)
        records.append(record)

    for directory in directories:
        committed = _review_store.committed_review_root(directory.name)
        if committed is None:
            continue
        plan_root = committed / _review_store.COMMITTED_PLAN_DIRNAME
        if not plan_root.is_dir():
            continue
        for slug_dir in sorted(plan_root.iterdir()):
            if not slug_dir.is_dir():
                continue
            for path in sorted(slug_dir.glob("*.json")):
                _collect(path, directory.name)
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(PLAN_REVIEW_FILE_GLOB)):
            _collect(path, directory.name)
    records.sort(
        key=lambda item: (str(item.get("timestamp") or ""), item["review_path"])
    )
    return records


def is_design_review(record: Mapping[str, Any]) -> bool:
    """Whether a stored record was composed under the design rubric.

    A record spells its rubric as the verb's flag or as the item-set name the
    report parser resolves it to; both spellings are read. A record naming no
    rubric is not counted, because nothing shows a reviewer searched the
    codebase for it.
    """
    rubric = str(record.get("rubric") or "").strip()
    return rubric in _DESIGN_RUBRIC_NAMES


def newest_design_review(
    project: str,
    plan_slug: str,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """The newest stored design review of a plan at any version, or None.

    A plan carries one prior-art and depth review before its first
    implementation node, and it is not repeated for each later edit: an edit
    is a revision of a design already searched against the codebase, which the
    content review that covers it reads. So any version counts here, while
    coverage of the content about to be built is judged separately.
    """
    records = [
        record
        for record in list_plan_reviews(project, base_dir=base_dir)
        if record.get("plan_slug") == plan_slug and is_design_review(record)
    ]
    return max(
        records, key=lambda record: int(record.get("plan_version") or 0), default=None
    )


def finding_ids(record: Mapping[str, Any]) -> list[str]:
    """Return the ids of the findings a review record carries, in order.

    A finding is a mapping with an ``id``; one without an id cannot be answered
    or counted, so it is skipped rather than given an invented identity that
    would not survive a re-read of the same record.
    """
    findings = record.get("findings")
    if not isinstance(findings, list):
        return []
    ids: list[str] = []
    for finding in findings:
        if isinstance(finding, Mapping):
            found = str(finding.get("id") or "").strip()
            if found:
                ids.append(found)
    return ids


def unanswered_findings(record: Mapping[str, Any]) -> list[str]:
    """Return the finding ids that carry no response yet.

    The dispatch gate keys on this: a review satisfies the gate when this list
    is empty. A response is present when it names an action; a response with no
    action is not an answer, so it does not close its finding.
    """
    responses = record.get("responses")
    answered = set(responses) if isinstance(responses, Mapping) else set()
    return [
        finding_id for finding_id in finding_ids(record) if finding_id not in answered
    ]


def record_response(
    record: Mapping[str, Any],
    finding_id: str,
    *,
    action: str,
    reason: str | None = None,
    by: str = "",
    when: str | None = None,
    base_dir: str | Path | None = None,
) -> Path:
    """Answer one finding on a stored review and persist the updated record.

    ``action`` is ``acted`` or ``declined``. A decline must carry a one-line
    ``reason``: the decision makes the reason the record, so a reasonless
    decline is refused before anything is written — the stored file is left
    untouched and the raise names what was missing. The response is keyed by
    ``finding_id``, which must name a finding the record carries, so an answer
    can never attach to a finding nobody can read back. The returned path is the
    record whose ``responses`` now carries the answer.
    """
    action_value = str(action or "").strip().lower()
    if action_value not in RESPONSE_ACTIONS:
        raise ValueError(f"response action {action!r} is not one of {RESPONSE_ACTIONS}")
    known = finding_ids(record)
    if str(finding_id) not in known:
        raise ValueError(
            f"finding {finding_id!r} is not on this review; known: {known}"
        )
    reason_value = str(reason or "").strip()
    if action_value == "declined" and not reason_value:
        raise ValueError("a declined finding needs a one-line reason")
    response = {
        "action": action_value,
        "reason": reason_value,
        "by": str(by or ""),
        "when": when or datetime.now(UTC).isoformat(),
    }
    updated = dict(record)
    responses = dict(updated.get("responses") or {})
    responses[str(finding_id)] = response
    updated["responses"] = responses
    # The answer is appended to an event list as well as written into the
    # latest-per-finding map. The map is what the dispatch gate reads, but it
    # rewrites an answer in place, so the history of what was answered and when
    # survives only here. The record's findings and every other body field are
    # carried through untouched: only these two keys move.
    events = list(updated.get(RESPONSE_EVENTS_KEY) or [])
    events.append({"finding": str(finding_id), **response})
    updated[RESPONSE_EVENTS_KEY] = events
    return store_plan_review(updated, base_dir=base_dir)


def declined_recurrence(
    *,
    base_dir: str | Path | None = None,
    threshold: int = RECURRENCE_THRESHOLD,
) -> dict[str, dict[str, Any]]:
    """Count, per finding type, the distinct plans whose author declined it.

    A finding type declined across ``threshold`` or more distinct plans is
    surfaced — the repeated decline means either the rubric or a practice is
    wrong, and the lead rules on it. Distinct plans are counted, not declines:
    two declines of one type in one plan are one plan's judgement, not
    recurrence, so the same plan contributes once whatever it carries. Each
    returns ``{plans, plan_count, surfaced}``.
    """
    declined: dict[str, set[str]] = {}
    for record in list_plan_reviews(base_dir=base_dir):
        responses = record.get("responses")
        findings = record.get("findings")
        if not isinstance(responses, Mapping) or not isinstance(findings, list):
            continue
        plan_key = f"{record.get('project')}/{record.get('plan_slug')}"
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            finding_id = str(finding.get("id") or "").strip()
            finding_type = str(finding.get("type") or "").strip()
            response = responses.get(finding_id) if finding_id else None
            if not finding_type or not isinstance(response, Mapping):
                continue
            if str(response.get("action") or "").strip().lower() != "declined":
                continue
            declined.setdefault(finding_type, set()).add(plan_key)
    recurrence: dict[str, dict[str, Any]] = {}
    for finding_type in sorted(declined):
        plans = sorted(declined[finding_type])
        recurrence[finding_type] = {
            "plans": plans,
            "plan_count": len(plans),
            "surfaced": len(plans) >= threshold,
        }
    return recurrence


# ── Delivered review reports ────────────────────────────────────────────────
# A plan review is dispatched as a run that emits its RUBRIC and FINDING lines
# into a report file, with a sidecar beside it naming the plan content it
# composed for. The report is the reviewer's raw output and the sidecar is what
# joins it to the plan; the record the dispatch gate reads is built from the
# two. Storing is keyed to the run that produced the report and is idempotent,
# so the gate can store a delivered report the coordinator never stored itself.

_REVIEW_REPORT_NAME = "report.md"
_REVIEW_SIDECAR_NAME = "plan-review.json"
# The snapshot is the plan's ``plan.html`` copied beside the report when a
# review is dispatched. It is the one durable copy of the plan content a stored
# review composed for, so coverage recomputes a review's unit digests from it
# and the store's definition of what a digest covers can change without
# re-reviewing a plan nobody touched. The writer in :mod:`reckon.crew.recovery`
# reads this spelling rather than restating it.
_REVIEW_SNAPSHOT_NAME = "plan.html"

# The report grammar (RUBRIC and FINDING lines) is parsed by
# reckon.crew.review.parse_plan_review_report, re-exported above, so the
# code-review and plan-report grammars read reviewer text through one reader.


def _plan_review_report_root(project: str, plan_slug: str) -> Path:
    """Return the plan-level review-report directory both readers build on.

    One owner for the report-directory layout: :func:`review_report_directory`
    extends it with a run id and :func:`delivered_reports` lists it, so the
    ``plan-review`` path segment is spelled here alone.
    """
    from reckon.crew.runs import reports_dir

    return reports_dir() / project / "plan-review" / plan_slug


def review_report_directory(project: str, plan_slug: str, run_id: str) -> Path:
    """Return the directory a review run writes its report and sidecar into."""
    return _plan_review_report_root(project, plan_slug) / run_id


def write_review_sidecar(
    directory: Path,
    *,
    project: str | None = None,
    plan_slug: str | None = None,
    plan_version: int | None = None,
    reviewed_blob_sha: str | None = None,
    document: Mapping[str, Any] | str | Path | None = None,
    rubric: str | None = None,
    report_path: Path | None = None,
    payload: Mapping[str, Any] | None = None,
    store_error: str | None = None,
) -> Path:
    """Write the sidecar that joins a report to the plan content it read.

    The sidecar names what the review composed for — the plan slug, version,
    reviewed blob sha and content fingerprint, and the rubric — so the record is
    keyed to what the reviewer read rather than to what the plan says later. It
    is written beside the report through the shared atomic writer, so a reader
    never meets a half-written sidecar.

    The shape is built from the named fields, or carried back verbatim when
    ``payload`` is given, which is how a later reader that only adds a key
    rewrites the delivery's own bytes rather than re-deriving them. A store
    that refuses the delivery records its reason under ``store_error``; a
    delivery stored cleanly carries no such key.
    """
    directory = Path(directory)
    if payload is None:
        payload = {
            "project": str(project),
            "plan_slug": str(plan_slug),
            "plan_version": int(plan_version),
            "reviewed_blob_sha": str(reviewed_blob_sha),
            "plan_fingerprint": plan_fingerprint(document),
            "section_digests": _section_digests(document),
            "rubric": str(rubric),
            "report_path": str(Path(report_path)),
        }
    else:
        payload = dict(payload)
    if store_error is not None:
        payload["store_error"] = str(store_error)
    path = directory / _REVIEW_SIDECAR_NAME
    write_json_atomically(path, payload, indent=2, sort_keys=True, mode=None)
    return path


def _stored_review_run_ids(project: str) -> set[str]:
    """Return the review-run ids the stored records for ``project`` carry."""
    ids: set[str] = set()
    for record in list_plan_reviews(project=project):
        run_id = str(record.get("review_run_id") or "").strip()
        if run_id:
            ids.add(run_id)
    return ids


def delivered_reports(
    project: str,
    plan_slug: str,
    *,
    plan_fingerprint: str | Iterable[str] | None = None,
    plan: Mapping[str, Any] | str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return the delivered review reports under a plan, newest first.

    A delivered report is a run directory carrying both ``report.md`` and its
    ``plan-review.json`` sidecar. Each returned dict is the sidecar plus
    ``report_path`` (the report file's path), ``review_run_id`` (the run
    directory's name), and ``stored`` — whether a stored record already carries
    that run id, so a composed report is not re-stored. A named
    ``plan_fingerprint`` — one digest or a set of digests, so a report stored
    under the legacy definition is still found — keeps only the reports whose
    sidecar carries one of them, which is how the gate finds the report for the
    content about to be built.
    ``plan`` accepts a path, document or state and selects both current and
    legacy fingerprints, taking precedence over ``plan_fingerprint``.
    """
    root = _plan_review_report_root(project, plan_slug)
    if not root.is_dir():
        return []
    stored = _stored_review_run_ids(project)
    wanted = (
        _fingerprint_forms(plan)
        if plan is not None
        else _wanted_fingerprints(plan_fingerprint)
    )
    found: list[dict[str, Any]] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        sidecar_path = directory / _REVIEW_SIDECAR_NAME
        report_path = directory / _REVIEW_REPORT_NAME
        if not (sidecar_path.is_file() and report_path.is_file()):
            continue
        sidecar = _load(sidecar_path)
        if sidecar is None:
            continue
        if (
            wanted is not None
            and str(sidecar.get("plan_fingerprint") or "") not in wanted
        ):
            continue
        entry = dict(sidecar)
        entry["report_path"] = str(report_path)
        entry["review_run_id"] = str(
            sidecar.get("review_run_id") or directory.name
        ).strip()
        entry["stored"] = entry["review_run_id"] in stored
        entry["_mtime_ns"] = report_path.stat().st_mtime_ns
        found.append(entry)
    found.sort(key=lambda item: item["_mtime_ns"], reverse=True)
    for entry in found:
        entry.pop("_mtime_ns", None)
    return found


def _store_delivered_report(
    sidecar: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """Parse the report a sidecar names and store its record, returning the path.

    The sidecar supplies the plan identity, the reviewed blob sha and the
    content fingerprint; the report named by ``report_path`` supplies the
    rubric's verdicts and findings. The record carries an empty ``responses``,
    so its findings arrive unanswered and the gate refuses until each is
    answered. ``review_run_id`` is the report directory's name, which is what
    makes a re-read of the same delivered report idempotent. The write goes
    through :func:`store_plan_review`, so it is atomic and keyed the same way
    every stored review is.
    """
    report_path = Path(str(sidecar.get("report_path") or ""))
    if not report_path.is_file():
        raise ValueError(f"delivered review report {report_path} does not exist")
    plan_version = sidecar.get("plan_version")
    if plan_version is None:
        raise ValueError("delivered review sidecar is missing plan_version")
    rubric = str(sidecar.get("rubric") or "").strip()
    parsed = parse_review_report(
        report_path.read_text(encoding="utf-8", errors="replace"), rubric=rubric
    )
    record = {
        "project": str(sidecar.get("project") or "").strip(),
        "plan_slug": str(sidecar.get("plan_slug") or "").strip(),
        "plan_version": int(plan_version),
        "rubric": rubric,
        "reviewed_blob_sha": str(sidecar.get("reviewed_blob_sha") or ""),
        "plan_fingerprint": str(sidecar.get("plan_fingerprint") or ""),
        "section_digests": dict(sidecar.get("section_digests") or {}),
        "findings": parsed["findings"],
        "responses": {},
        "status": DEFAULT_STATUS,
        "review_run_id": report_path.parent.name,
        "rubric_items": parsed["rubric_items"],
        "absent_items": parsed["absent_items"],
    }
    return store_plan_review(record, base_dir=base_dir)


def store_delivered_reviews(
    project: str,
    plan_slug: str,
    *,
    base_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Store every unstored delivered review for a plan, recording refusals.

    A delivered review is a report a review run left on the durable report path
    with a sidecar beside it. It is stored at the moment the plan is built, so
    the coordinator never runs a store step; a delivery the store refuses is
    recorded on its own sidecar, under ``store_error``, with the reason, rather
    than skipped, so the next reader sees why the review did not take rather
    than the gate's missing-review symptom. A delivery that stores on a later
    pass has that reason cleared from its sidecar, so the file never contradicts
    the stored review beside it. The returned list names each refused delivery
    and its reason, for a caller that reports them.
    """
    refused: list[dict[str, Any]] = []
    for sidecar in delivered_reports(project, plan_slug):
        if sidecar.get("stored"):
            continue
        try:
            _store_delivered_report(sidecar, base_dir=base_dir)
        except (OSError, ValueError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            _record_store_error(sidecar, reason)
            refused.append(
                {"review_run_id": sidecar.get("review_run_id"), "store_error": reason}
            )
        else:
            _clear_store_error(sidecar)
    return refused


def _record_store_error(sidecar: Mapping[str, Any], reason: str) -> None:
    """Write a refused delivery's reason onto its own sidecar.

    The delivery's existing payload is read back and rewritten through the one
    sidecar writer with ``store_error`` added, so the shape keeps one spelling
    and a later reader reads the cause off the file rather than its absence. A
    sidecar that cannot be read leaves nothing to amend and is skipped.
    """
    report_path = Path(str(sidecar.get("report_path") or ""))
    directory = report_path.parent
    payload = _load(directory / _REVIEW_SIDECAR_NAME)
    if payload is None:
        return
    write_review_sidecar(directory, payload=payload, store_error=reason)


def _clear_store_error(sidecar: Mapping[str, Any]) -> None:
    """Remove a stale ``store_error`` from a delivery that has now stored.

    A delivery refused once and stored cleanly on a later pass would otherwise
    keep a reason for a refusal that no longer holds, contradicting the stored
    review beside it. The sidecar is read back and rewritten through the one
    sidecar writer without the key; a sidecar that carries no ``store_error`` is
    left untouched, so a clean store writes nothing.
    """
    report_path = Path(str(sidecar.get("report_path") or ""))
    directory = report_path.parent
    payload = _load(directory / _REVIEW_SIDECAR_NAME)
    if payload is None or "store_error" not in payload:
        return
    payload.pop("store_error", None)
    write_review_sidecar(directory, payload=payload)
