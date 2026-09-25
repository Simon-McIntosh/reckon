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
followups, relationships and comments. The parser derives a little more state
from those scalars, so :data:`PLAN_DERIVED_SCALARS` removes that too; without
it, adding the named effort field to a plan that carried only the legacy effort
letter would move the fingerprint through the derived calibration flag and
orphan a review on exactly the plans that predate the named field. A metadata-only write
therefore neither triggers a review nor invalidates one, and an authored edit
changes the fingerprint so the gate demands a fresh review.

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


def plan_fingerprint(plan: Mapping[str, Any] | str | Path) -> str:
    """Return the content fingerprint that joins a review to the plan it read.

    Two components are digested. The parsed state has the excluded keys of
    :data:`PLAN_METADATA_SCALARS` and the derived keys of
    :data:`PLAN_DERIVED_SCALARS` removed, and the remainder canonicalised with
    sorted keys. The document's authored prose — the body text outside every
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
    payload: dict[str, Any] = {
        "state": {
            str(key): value
            for key, value in _as_state(plan).items()
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
    :data:`DEFAULT_STATUS` on the stored copy. The write is atomic: a temporary
    file is replaced into place, so a reader never sees a half-written record.
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
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
