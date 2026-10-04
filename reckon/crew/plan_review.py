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
flag — and digests the authored content: declarations, sections, decisions,
followups, relationships and comments, except the landing comments a promotion
writes for a run, which record that work landed rather than an authored change
and are normalised out beside the metadata scalars. The parser derives a little
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

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from reckon import _plan_html
from reckon._store import write_json_atomically
from reckon.crew import review as _review_store

# ── The fingerprint exclusion set ───────────────────────────────────────────
# These are the server-managed metadata scalars: written by the store or the web
# surface, never authored as plan content. A change to any of them neither
# requires a review nor invalidates one, so they are normalised out of the
# digest. Every other key of the parsed plan is authored content and stays in.
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
PLAN_DERIVED_SCALARS: tuple[str, ...] = (
    "effort_calibrated",
    "compatibility_warnings",
)

# A review is taken under one of two rubrics: the section 2 rubber duck of a
# plan's authored content, and the section 3 prior-art-and-depth review the
# first implementation dispatch of a plan additionally requires.
PLAN_REVIEW_RUBRICS: tuple[str, ...] = ("plan_review", "plan_design_review")

# ── The promotion-comment exclusion ─────────────────────────────────────────
# A promotion appends one comment per promoted run to the section the run
# landed against, under an id derived from the run id and beginning with this
# prefix. The comment is the plan's own record that work landed, written by
# machinery rather than authored, so it is normalised out of the digest beside
# the metadata scalars: without that, every promotion moves a reviewed plan's
# fingerprint and buys the plan another review of content nobody changed.
RUN_COMMENT_PREFIX = "c-run-"

# The states the surface renders beside the version a review read. ``ready`` is
# what a freshly stored review carries; ``acted`` and ``declined`` are reached
# once every finding is answered; ``pending`` is the surface's state for a
# version with no stored review yet.
PLAN_REVIEW_STATUSES: tuple[str, ...] = ("pending", "ready", "acted", "declined")
DEFAULT_STATUS = "ready"

# An author answers each finding by acting on it or by declining it. Either
# answer closes the finding for the dispatch gate; a decline must carry a reason.
RESPONSE_ACTIONS: tuple[str, ...] = ("acted", "declined")

# A finding type declined across this many distinct plans surfaces to the lead.
RECURRENCE_THRESHOLD = 3

_BLOB_RE = re.compile(r"[0-9A-Fa-f]{7,64}")

# Tags whose content is never authored prose, and the void elements that carry
# no text and would otherwise leave an unbalanced protected-stack behind.
_PROSE_SKIP_TAGS = frozenset({"script", "style", "head"})
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _AuthoredProseParser(HTMLParser):
    """Collect a plan document's authored prose.

    The parsed state carries no section prose: :func:`reckon._plan_html.read_state`
    reads the metas, the typed section records and the reckon-owned blocks, and
    the paragraphs a reader sees under each heading are never part of it. A
    fingerprint over state alone would therefore not move when a plan's prose is
    rewritten, leaving the one edit a review exists to catch invisible.

    So the digest also covers the document's authored text — what lies in the
    body outside every element carrying ``data-reckon="..."``, which are the
    blocks :func:`reckon._plan_html.write_state` regenerates from state and which
    the state component already covers canonically. The head is skipped, so a
    metadata-only write cannot reach the prose through ``<title>`` or a meta.
    """

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[bool] = []
        self._in_body = False
        self._chunks: list[str] = []

    def _protected(self) -> bool:
        return any(self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            return
        if tag == "body":
            self._in_body = True
        carries_reckon = any(name == "data-reckon" for name, _value in attrs)
        self._stack.append(
            self._protected() or carries_reckon or tag in _PROSE_SKIP_TAGS
        )

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_body and not self._protected():
            self._chunks.append(data)

    def prose(self) -> str:
        """Return the collected prose, whitespace-normalised."""
        return " ".join("".join(self._chunks).split())


def _authored_prose(html: str) -> str:
    """Return the collected authored prose of a plan document."""
    parser = _AuthoredProseParser()
    parser.feed(html)
    parser.close()
    return parser.prose()


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


def _without_run_comments(state: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return parsed state with the promotion comments a run wrote removed.

    ``comments`` maps a section id to the records anchored to it, and a
    promotion appends one record per promoted run under an id beginning with
    :data:`RUN_COMMENT_PREFIX`. Nothing authored changes when a landing is
    recorded, so those records are dropped — and a section left empty is
    dropped with them, so a plan holding only run comments digests exactly as
    one holding none. Every record written for another reason stays in and
    moves the digest.

    A state carrying no ``comments`` key, or one whose value is not the parsed
    mapping shape, is returned untouched: the normalisation removes records it
    can recognise and never hides one it cannot classify.
    """
    comments = state.get("comments")
    if "comments" not in state or not isinstance(comments, Mapping):
        return state
    kept: dict[str, Any] = {}
    for section, entries in comments.items():
        kept_entries = entries
        if isinstance(entries, (list, tuple)):
            kept_entries = [
                entry
                for entry in entries
                if not (
                    isinstance(entry, Mapping)
                    and str(entry.get("id") or "").startswith(RUN_COMMENT_PREFIX)
                )
            ]
        if kept_entries:
            kept[str(section)] = kept_entries
    return {**state, "comments": kept}


def plan_fingerprint(plan: Mapping[str, Any] | str | Path) -> str:
    """Return the content fingerprint that joins a review to the plan it read.

    Two components are digested. The parsed state has the excluded keys of
    :data:`PLAN_METADATA_SCALARS` and the derived keys of
    :data:`PLAN_DERIVED_SCALARS` removed, and the remainder canonicalised with
    sorted keys; the run comments of :data:`RUN_COMMENT_PREFIX` are removed
    with them, because a landing record is written by a promotion rather than
    authored. The document's authored prose — the body text outside every
    ``data-reckon`` block, which the store regenerates from state — is folded in
    beside it, because the parsed state carries no section prose and a
    fingerprint over state alone would not move when a plan's prose is
    rewritten.

    A caller passing a mapping passes a state and nothing more, so the digest
    covers state alone; a path or the document text gives the full fingerprint.
    With the excluded keys normalised out, an authored edit changes the digest
    and a metadata-only write does not. The result is a hex sha256 string.
    """
    if not isinstance(plan, (Mapping, str, Path)):
        raise TypeError(
            f"plan_fingerprint expects a mapping, path or html, got {type(plan)!r}"
        )
    excluded = frozenset(PLAN_METADATA_SCALARS) | frozenset(PLAN_DERIVED_SCALARS)
    digest_state = _without_run_comments(_as_state(plan))
    payload: dict[str, Any] = {
        "state": {
            str(key): value
            for key, value in digest_state.items()
            if key not in excluded
        }
    }
    document = _as_document(plan)
    if document is not None:
        payload["prose"] = _authored_prose(document)
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def plan_review_path(
    project: str,
    plan_slug: str,
    plan_version: int,
    base_dir: str | Path | None = None,
    *,
    reviewed_blob_sha: str | None = None,
) -> Path:
    """Return the durable path of a plan review, keyed by version and blob.

    ``reviews/<project>/plan-<slug>.v<N>.json`` is the primary path. A named
    ``reviewed_blob_sha`` selects the ``.at-<blob8>`` sibling the second review
    of one version lands on, so a re-review never overwrites its predecessor. An
    invalid blob naming is refused rather than silently normalised into a path.
    """
    suffix = ""
    if reviewed_blob_sha is not None:
        blob = str(reviewed_blob_sha).strip()
        if not _BLOB_RE.fullmatch(blob):
            raise ValueError(f"invalid reviewed_blob_sha {reviewed_blob_sha!r}")
        suffix = f".at-{blob.lower()[:8]}"
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
    if plan_version is None:
        raise ValueError("plan review record is missing plan_version")
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

    A named version restricts the search to that version's files; an unnamed
    version spans every version of the plan, so a caller asking "is this plan
    reviewed at this fingerprint?" searches across versions rather than guessing
    which one the stored review named.
    """
    root = _review_store.review_store_root(base_dir) / project
    if not root.is_dir():
        return []
    if plan_version is not None:
        patterns = [f"plan-{plan_slug}.v{int(plan_version)}.json"]
        patterns.append(f"plan-{plan_slug}.v{int(plan_version)}.at-*.json")
    else:
        patterns = [f"plan-{plan_slug}.v*.json"]
    found: dict[str, Path] = {}
    for pattern in patterns:
        for path in root.glob(pattern):
            found[str(path)] = path
    return sorted(found.values())


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
    plan_fingerprint: str | None = None,
    reviewed_blob_sha: str | None = None,
) -> dict[str, Any] | None:
    """Return the newest stored plan review matching the given filters.

    ``plan_version`` restricts the search to one version when named. A
    ``plan_fingerprint`` filter selects the review of the content being asked
    about, which is how the dispatch gate joins a review to the plan it will
    build: the fingerprint, not the version integer, is the key that survives an
    impl bump. A ``reviewed_blob_sha`` filter selects a named content revision.
    Among the records that pass the filters the newest by file mtime is
    returned; ``None`` means no stored review matches.
    """
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in _candidate_paths(project, plan_slug, plan_version, base_dir):
        record = _load(path)
        if record is None:
            continue
        if plan_fingerprint is not None and (
            str(record.get("plan_fingerprint") or "") != str(plan_fingerprint)
        ):
            continue
        if reviewed_blob_sha is not None:
            named = str(reviewed_blob_sha).strip().lower()
            carried = _record_blob8(record) or ""
            if not carried or not (
                carried.startswith(named) or named.startswith(carried)
            ):
                continue
        records.append((path, record))
    if not records:
        return None
    return max(records, key=lambda item: item[0].stat().st_mtime_ns)[1]


def list_plan_reviews(
    project: str | None = None,
    *,
    base_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return every stored plan review, newest last.

    Code reviews share the store root with plan reviews; only files named
    ``plan-<slug>.v<N>.json`` (and their ``.at-`` siblings) are plan reviews, so
    a listing never mixes the two populations. ``project`` restricts the walk to
    one project directory; omitted, every project under the root is read. Each
    returned record carries its ``review_path`` so a caller can rewrite the very
    file it read.
    """
    root = _review_store.review_store_root(base_dir)
    if project is not None:
        directories = [root / project]
    elif root.is_dir():
        directories = sorted(entry for entry in root.iterdir() if entry.is_dir())
    else:
        directories = []
    records: list[dict[str, Any]] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("plan-*.v*.json")):
            record = _load(path)
            if record is None:
                continue
            record.setdefault("project", directory.name)
            record["review_path"] = str(path)
            records.append(record)
    records.sort(
        key=lambda item: (str(item.get("timestamp") or ""), item["review_path"])
    )
    return records


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
    updated = dict(record)
    responses = dict(updated.get("responses") or {})
    responses[str(finding_id)] = {
        "action": action_value,
        "reason": reason_value,
        "by": str(by or ""),
        "when": when or datetime.now(UTC).isoformat(),
    }
    updated["responses"] = responses
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

# The rubric name selects the checklist items a report is judged against. Both
# item sets are owned by reckon.crew.review and mirrored into the two prompt
# files under prompts/, so they are read from there rather than copied. The
# dispatch flag spells the two rubrics ``content`` and ``design``; both aliases
# are accepted so a sidecar written from either reaches the same item set.
_REPORT_RUBRIC_ITEMS: dict[str, tuple[str, ...]] = {
    "plan_review": _review_store.PLAN_REVIEW_ITEMS,
    "plan_design_review": _review_store.PLAN_DESIGN_REVIEW_ITEMS,
}
_REPORT_RUBRIC_ALIASES: dict[str, str] = {
    "content": "plan_review",
    "design": "plan_design_review",
}

_REVIEW_REPORT_NAME = "report.md"
_REVIEW_SIDECAR_NAME = "plan-review.json"

# The emitted form the two prompts ask for is one line per element:
#     RUBRIC <item>: <pass, or the finding it produced — one sentence>
#     FINDING <item> <file>:<line> — <what is wrong and why> — WOULD_CHANGE_THE_PLAN: <yes|no> — REASON: <one line>
# The anchor is a ``<file>:<line>`` or a ``<plan>#<node>`` reference. The
# would-change verdict and its reason are the record: a finding without the
# verdict cannot be scored, so a finding line that carries no parseable verdict
# is still returned the id, type, anchor and text with ``would_change`` left
# ``None`` rather than defaulted. Lines in any other shape are ignored, so the
# reviewer may surround the emitted lines with prose without breaking the parse.
_RUBRIC_LINE_RE = re.compile(
    r"^RUBRIC\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", re.IGNORECASE
)
_FINDING_LINE_RE = re.compile(r"^FINDING\s+(\S+)\s+(.+)$", re.IGNORECASE)
_FINDING_TAIL_RE = re.compile(
    r"^(?P<anchor>.*?)\s+(?:—|--)\s+(?P<text>.*?)\s+(?:—|--)\s+"
    r"WOULD_CHANGE_THE_PLAN\s*:\s*(?P<would_change>yes|no)\b"
    r"(?:\s+(?:—|--)\s+REASON\s*:\s*(?P<reason>.*))?$",
    re.IGNORECASE,
)


def _report_rubric_items(rubric: str) -> tuple[str, ...]:
    """Return the checklist items a report under ``rubric`` is judged against."""
    name = _REPORT_RUBRIC_ALIASES.get(str(rubric or "").strip().lower(), "")
    name = name or str(rubric or "").strip().lower()
    items = _REPORT_RUBRIC_ITEMS.get(name)
    if items is None:
        raise ValueError(
            f"unknown review rubric {rubric!r}; known rubrics are "
            f"{', '.join(PLAN_REVIEW_RUBRICS)}"
        )
    return items


def _parse_finding(item: str, remainder: str) -> dict[str, Any]:
    """Parse one FINDING line's tail into a finding, per the prompt grammar.

    The structured form carries the anchor, the text, the would-change verdict
    and its reason. A line that does not carry the verdict is accepted with
    ``would_change`` left ``None`` — an unstated verdict is a recorded absence,
    not a defaulted judgement — and the anchor is read as the leading token.
    """
    match = _FINDING_TAIL_RE.match(remainder)
    if match:
        return {
            "anchor": match.group("anchor").strip(),
            "text": match.group("text").strip(),
            "would_change": match.group("would_change").lower() == "yes",
            "reason": (match.group("reason") or "").strip(),
        }
    anchor, separator, text = remainder.partition(" ")
    if not separator:
        anchor, text = remainder, ""
    return {
        "anchor": anchor.strip(),
        "text": text.strip(),
        "would_change": None,
        "reason": "",
    }


def parse_review_report(text: str, *, rubric: str) -> dict[str, Any]:
    """Parse a delivered review report into the store's record shape.

    The rubric selects the checklist items the report is judged against, and
    both item sets come from reckon.crew.review. Returns:

    - ``rubric`` — the rubric the report was parsed under.
    - ``rubric_items`` — item to the verdict sentence its ``RUBRIC`` line
      carried, for the items that were emitted with text.
    - ``absent_items`` — checklist items with no ``RUBRIC`` line, or with one
      carrying no text: a line that says nothing is not a verdict, so the item
      is reported absent rather than silently taken as checked.
    - ``findings`` — a list of ``{"id", "type", "anchor", "text",
      "would_change", "reason"}``, one per ``FINDING`` line whose item belongs
      to the rubric and which is a mapping. The id is ``<item>-<n>`` with ``n``
      counting the report's findings of that item in the order they appear, so
      an id is stable across a re-read of the same report and two findings of
      one type never share an id.
    """
    items = _report_rubric_items(rubric)
    rubric_items: dict[str, str] = {}
    findings: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        match = _RUBRIC_LINE_RE.match(line)
        if match:
            item = match.group(1).lower()
            verdict = match.group(2).strip()
            if item in items and verdict:
                rubric_items[item] = verdict
            continue
        match = _FINDING_LINE_RE.match(line)
        if match:
            item = match.group(1).lower()
            if item not in items:
                continue
            parsed = _parse_finding(item, match.group(2).strip())
            counters[item] = counters.get(item, 0) + 1
            parsed["id"] = f"{item}-{counters[item]}"
            parsed["type"] = item
            findings.append(parsed)
            continue
    absent_items = [item for item in items if item not in rubric_items]
    return {
        "rubric": rubric,
        "rubric_items": rubric_items,
        "absent_items": absent_items,
        "findings": findings,
    }


def review_report_directory(project: str, plan_slug: str, run_id: str) -> Path:
    """Return the directory a review run writes its report and sidecar into."""
    from reckon.crew.runs import reports_dir

    return reports_dir() / project / "plan-review" / plan_slug / run_id


def write_review_sidecar(
    directory: Path,
    *,
    project: str,
    plan_slug: str,
    plan_version: int,
    reviewed_blob_sha: str,
    plan_fingerprint: str,
    rubric: str,
    report_path: Path,
) -> Path:
    """Write the sidecar that joins a report to the plan content it read.

    The sidecar names what the review composed for — the plan slug, version,
    reviewed blob sha and content fingerprint, and the rubric — so the record is
    keyed to what the reviewer read rather than to what the plan says later. It
    is written beside the report through the shared atomic writer, so a reader
    never meets a half-written sidecar.
    """
    directory = Path(directory)
    payload = {
        "project": str(project),
        "plan_slug": str(plan_slug),
        "plan_version": int(plan_version),
        "reviewed_blob_sha": str(reviewed_blob_sha),
        "plan_fingerprint": str(plan_fingerprint),
        "rubric": str(rubric),
        "report_path": str(Path(report_path)),
    }
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
    plan_fingerprint: str | None = None,
) -> list[dict[str, Any]]:
    """Return the delivered review reports under a plan, newest first.

    A delivered report is a run directory carrying both ``report.md`` and its
    ``plan-review.json`` sidecar. Each returned dict is the sidecar plus
    ``report_path`` (the report file's path), ``review_run_id`` (the run
    directory's name), and ``stored`` — whether a stored record already carries
    that run id, so a composed report is not re-stored. A named
    ``plan_fingerprint`` keeps only the reports whose sidecar carries it, which
    is how the gate finds the report for the content about to be built.
    """
    from reckon.crew.runs import reports_dir

    root = reports_dir() / project / "plan-review" / plan_slug
    if not root.is_dir():
        return []
    stored = _stored_review_run_ids(project)
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
        if plan_fingerprint is not None and (
            str(sidecar.get("plan_fingerprint") or "") != str(plan_fingerprint)
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


def store_delivered_report(
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
        "findings": parsed["findings"],
        "responses": {},
        "status": DEFAULT_STATUS,
        "review_run_id": report_path.parent.name,
        "rubric_items": parsed["rubric_items"],
        "absent_items": parsed["absent_items"],
    }
    return store_plan_review(record, base_dir=base_dir)
