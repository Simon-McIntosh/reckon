"""Distributed project-state resources and legacy-index compatibility.

Project workflow state is stored in independently versioned resources:

* ``docs/sprints/<id>.html``
* ``docs/milestones/<id>.html``
* ``docs/blockers/<id>.html``
* ``docs/state/<project>/timeline.html``
* ``docs/state/<project>/project.json``

The legacy ``index.json`` remains canonical only for an unmarked compatibility
corpus and is retained byte-for-byte in historical receipts.  A completion
marker is the format switch: with it, distributed resources are canonical and
missing or malformed resources are errors.  There are no dual writes.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from reckon._schema import (
    SPRINT_STATUS_ENUM,
    Blocker,
    Milestone,
    NorthStar,
    Sprint,
    TimelineEntry,
    parse_plan_ref,
)
from reckon.lifecycle import COMPLETED_STATUSES

MARKER_RELATIVE = Path(".reckon/project-state-migration.json")
RESOURCE_SCRIPT_ID = "reckon-resource-state"
RESOURCE_TYPES = frozenset(
    {"sprint", "milestone", "blocker", "timeline", "project", "review"}
)
LIFECYCLE_ITEM_FIELDS = frozenset({"status", "impl"})
NORTH_STAR_ADVISORY_CAP = 5
MIGRATION_COMPOSED_DERIVATIONS = frozenset(
    {
        "blockers[].id",
        "blockers[].next",
        "timeline[].id",
    }
)
PROJECT_DERIVED_FIELDS = frozenset(
    {
        "active_sprint_id",
        "sprints",
        "milestones",
        "blockers",
        "timeline",
        "inventory",
        "projects",
        "plans_count",
        "active",
        "blocked",
        "pending",
        "shipped",
        "path",
        "type",
        "id",
        "version",
    }
)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_META_RE = re.compile(
    r"""<meta\b(?=[^>]*\bname=["'](?P<name>[^"']+)["'])(?=[^>]*\bcontent=["'](?P<content>[^"']*)["'])[^>]*>""",
    re.IGNORECASE,
)
_SCRIPT_RE = re.compile(
    rf"""<script\b[^>]*\bid=["']{RESOURCE_SCRIPT_ID}["'][^>]*>(?P<data>.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
_FINDING_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REVIEW_CATEGORIES = frozenset(
    {"sprint", "dag", "lifecycle", "provenance", "references", "calibration"}
)
_REVIEW_SEVERITIES = frozenset({"error", "warn", "info"})
_REVIEW_SUBJECT_KINDS = frozenset(
    {"plan", "sprint", "milestone", "blocker", "followup", "decision", "project"}
)
_REVIEW_ACTIONS = frozenset(
    {
        "close",
        "resequence",
        "rescope",
        "recalibrate",
        "resolve",
        "repair-pointer",
        "reopen",
    }
)
_REVIEW_VALIDATIONS = frozenset({"confirmed", "stale", "conflicting"})
_PRIORITY_REASONS = frozenset(
    {"critical-path", "unlock", "deadline", "roi", "decision-first"}
)


class ProjectStateError(RuntimeError):
    """Base error for distributed state contract violations."""


class LegacyIndexReadOnly(ProjectStateError):
    """Raised when a distributed project receives an aggregate index write."""


class ProjectStateConflict(ProjectStateError):
    """Optimistic-concurrency conflict on one distributed resource."""

    def __init__(self, expected: int, current: int, current_data: dict[str, Any]):
        self.expected = expected
        self.current = current
        self.current_data = current_data
        super().__init__(f"version conflict: expected {expected}, got {current}")


def _fsync_file(path: Path) -> None:
    """Flush a completed file before it becomes authoritative."""
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Flush directory metadata after a create, replace, or unlink."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_replace(source: Path, destination: Path) -> None:
    """Replace one file and durably publish its directory entry."""
    _fsync_file(source)
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _durable_unlink(path: Path) -> None:
    """Remove one file and durably publish the removal."""
    if not path.exists():
        return
    parent = path.parent
    path.unlink()
    _fsync_directory(parent)


@dataclass(frozen=True)
class ProjectStateMode:
    """Resolved canonical-format mode for one docs tree."""

    format: str
    marker: dict[str, Any] | None = None


def _safe_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."} or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} must be a single safe path segment")
    return value


def _repository_relative_path(value: Any, label: str) -> str:
    """Validate one stored repository-relative POSIX path without rewriting it."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a repository-relative POSIX path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ValueError(f"{label} must be a repository-relative POSIX path: {value!r}")
    return value


def marker_path(docs_dir: Path) -> Path:
    return docs_dir / MARKER_RELATIVE


def legacy_index_path(docs_dir: Path, project: str) -> Path:
    return docs_dir / "state" / _safe_segment(project, "project") / "index.json"


def resource_path(
    docs_dir: Path, project: str, resource_type: str, resource_id: str
) -> Path:
    """Return the canonical path for one distributed resource."""
    _safe_segment(project, "project")
    _safe_segment(resource_id, "resource id")
    if resource_type == "sprint":
        return docs_dir / "sprints" / f"{resource_id}.html"
    if resource_type == "milestone":
        return docs_dir / "milestones" / f"{resource_id}.html"
    if resource_type == "blocker":
        return docs_dir / "blockers" / f"{resource_id}.html"
    if resource_type == "timeline":
        if resource_id != "timeline":
            raise ValueError("timeline resource id must be 'timeline'")
        return docs_dir / "state" / project / "timeline.html"
    if resource_type == "review":
        if resource_id != "review":
            raise ValueError("review resource id must be 'review'")
        return docs_dir / "state" / project / "review.html"
    if resource_type == "project":
        if resource_id != "project":
            raise ValueError("project resource id must be 'project'")
        return docs_dir / "state" / project / "project.json"
    raise ValueError(f"unsupported project resource type {resource_type!r}")


@contextmanager
def _resource_lock(docs_dir: Path, project: str, resource_type: str, resource_id: str):
    """Serialise one resource's version check and atomic replacement."""
    _safe_segment(project, "lock project")
    _safe_segment(resource_type, "lock resource type")
    _safe_segment(resource_id, "lock resource id")
    lock_name = f"{project}-{resource_type}-{resource_id}.lock"
    lock_path = docs_dir / ".reckon" / "locks" / lock_name
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _resource_locks(docs_dir: Path, project: str, identities: list[tuple[str, str]]):
    """Acquire several resource locks in deterministic order."""
    with ExitStack() as stack:
        for resource_type, resource_id in sorted(set(identities)):
            stack.enter_context(
                _resource_lock(docs_dir, project, resource_type, resource_id)
            )
        yield


def project_state_mode(docs_dir: Path) -> ProjectStateMode:
    """Resolve legacy/distributed mode without silently falling back."""
    path = marker_path(docs_dir)
    if not path.exists():
        return ProjectStateMode("legacy")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectStateError(f"malformed project-state marker: {path}") from exc
    if not isinstance(marker, dict):
        raise ProjectStateError(f"malformed project-state marker: {path}")
    status = marker.get("status")
    if status == "staging":
        return ProjectStateMode("legacy", marker)
    if status != "complete" or marker.get("format") != "distributed":
        raise ProjectStateError(
            f"unsupported project-state marker status/format: {status!r}/"
            f"{marker.get('format')!r}"
        )
    return ProjectStateMode("distributed", marker)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _render_resource(
    project: str, resource_type: str, resource_id: str, data: dict[str, Any]
) -> str:
    """Render a semantic HTML resource with a canonical machine-readable island."""
    state = deepcopy(data)
    state["id"] = resource_id
    state["type"] = resource_type
    version = int(state.get("version", 0) or 0)
    title = (
        state.get("theme") or state.get("name") or state.get("summary") or resource_id
    )
    rows: list[str] = []
    if resource_type == "sprint":
        rows.append('<ol data-reckon="sprint-items">')
        for item in state.get("items", []):
            item = {"slug": item} if isinstance(item, str) else dict(item)
            slug = html.escape(str(item.get("slug", "")), quote=True)
            payload = html.escape(
                json.dumps(item, ensure_ascii=False, sort_keys=True), quote=True
            )
            rows.append(f'<li data-slug="{slug}" data-item="{payload}">{slug}</li>')
        rows.append("</ol>")
    elif resource_type == "timeline":
        rows.append('<ol data-reckon="timeline">')
        for event in state.get("events", []):
            eid = html.escape(str(event.get("id", "")), quote=True)
            payload = html.escape(
                json.dumps(event, ensure_ascii=False, sort_keys=True), quote=True
            )
            rows.append(f'<li data-id="{eid}" data-event="{payload}">{eid}</li>')
        rows.append("</ol>")
    elif resource_type == "review":
        rows.append('<ol data-reckon="review-findings">')
        for finding in state.get("findings", []):
            finding_id = html.escape(str(finding.get("id", "")), quote=True)
            code = html.escape(str(finding.get("code", "")))
            payload = html.escape(
                json.dumps(finding, ensure_ascii=False, sort_keys=True), quote=True
            )
            rows.append(
                f'<li data-id="{finding_id}" data-finding="{payload}">{code}</li>'
            )
        rows.append("</ol>")
    else:
        rows.append(
            f'<dl data-reckon="{resource_type}"><dt>Identity</dt>'
            f"<dd>{html.escape(resource_id)}</dd></dl>"
        )
    state_json = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).replace("</", "<\\/")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '  <meta charset="utf-8">\n'
        f'  <meta name="docs-project" content="{html.escape(project, quote=True)}">\n'
        f'  <meta name="reckon-type" content="{resource_type}">\n'
        f'  <meta name="reckon-id" content="{html.escape(resource_id, quote=True)}">\n'
        f'  <meta name="reckon-version" content="{version}">\n'
        f"  <title>{html.escape(str(title))} | {resource_type}</title>\n"
        "</head>\n<body>\n"
        f'<main class="reckon-resource" data-type="{resource_type}" '
        f'data-id="{html.escape(resource_id, quote=True)}">\n'
        + "\n".join(f"  {row}" for row in rows)
        + f'\n  <script type="application/json" id="{RESOURCE_SCRIPT_ID}">'
        + state_json
        + "</script>\n</main>\n</body>\n</html>\n"
    )


def _parse_resource(path: Path, expected_type: str, expected_id: str) -> dict[str, Any]:
    if not path.is_file():
        raise ProjectStateError(
            f"distributed {expected_type} resource is missing: {path}"
        )
    text = path.read_text(encoding="utf-8", errors="strict")
    match = _SCRIPT_RE.search(text)
    if not match:
        raise ProjectStateError(f"resource state island is missing: {path}")
    try:
        data = json.loads(match.group("data"))
    except json.JSONDecodeError as exc:
        raise ProjectStateError(f"resource state island is malformed: {path}") from exc
    if not isinstance(data, dict):
        raise ProjectStateError(f"resource state must be an object: {path}")
    if data.get("type") != expected_type or data.get("id") != expected_id:
        raise ProjectStateError(
            f"resource identity mismatch in {path}: "
            f"{data.get('type')!r}/{data.get('id')!r}"
        )
    return data


def _read_project_json(path: Path, project: str) -> dict[str, Any]:
    if not path.is_file():
        raise ProjectStateError(f"distributed project resource is missing: {path}")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectStateError(
            f"distributed project resource is malformed: {path}"
        ) from exc
    if not isinstance(envelope, dict) or envelope.get("project") != project:
        raise ProjectStateError(f"distributed project identity mismatch: {path}")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise ProjectStateError(f"distributed project data is malformed: {path}")
    return data


def read_resource(
    docs_dir: Path, project: str, resource_type: str, resource_id: str
) -> tuple[dict[str, Any], int]:
    """Read one distributed resource and its optimistic-concurrency version."""
    mode = project_state_mode(docs_dir)
    if mode.format != "distributed":
        raise ProjectStateError(
            "distributed_resource_inactive: project-state marker is not complete"
        )
    recover_project_state_transactions(docs_dir, project)
    data, version = _read_resource_unchecked(
        docs_dir, project, resource_type, resource_id
    )
    if resource_type == "sprint":
        warnings = _item_lifecycle_warnings([data])
        if warnings:
            data = deepcopy(data)
            data["compatibility_warnings"] = [
                *data.get("compatibility_warnings", []),
                *warnings,
            ]
    return data, version


def _read_resource_unchecked(
    docs_dir: Path, project: str, resource_type: str, resource_id: str
) -> tuple[dict[str, Any], int]:
    """Read without transaction recovery while the caller holds relevant locks."""
    if resource_type not in RESOURCE_TYPES:
        raise ValueError(f"unsupported project resource type {resource_type!r}")
    path = resource_path(docs_dir, project, resource_type, resource_id)
    if resource_type == "project":
        data = _read_project_json(path, project)
    else:
        data = _parse_resource(path, resource_type, resource_id)
    if resource_type == "review":
        data = deepcopy(data)
        for finding in data.get("findings", []):
            finding["status"] = "resolved" if finding.get("resolved_at") else "open"
    return data, int(data.get("version", 0) or 0)


def _review_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a YYYY-MM-DD date")
    return value


def _review_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _review_enum(value: Any, field: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(sorted(allowed))}")
    return str(value)


def _validate_review(data: dict[str, Any]) -> dict[str, Any]:
    reviewed_at = _review_date(data.get("reviewed_at"), "reviewed_at")
    reviewed_by = _review_text(data.get("reviewed_by"), "reviewed_by")
    basis = _review_text(data.get("basis"), "basis")
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        raise TypeError("findings must be a list")
    validated_findings: list[dict[str, Any]] = []
    finding_ids: set[str] = set()
    finding_fields = {
        "id",
        "code",
        "category",
        "severity",
        "subject",
        "evidence",
        "recommended_action",
        "validated",
        "checked_at",
        "resolved_at",
        "resolved_by",
        "outcome",
        "status",
    }
    for index, raw in enumerate(findings):
        field = f"findings[{index}]"
        if not isinstance(raw, dict):
            raise TypeError(f"{field} must be an object")
        unexpected = set(raw) - finding_fields
        if unexpected:
            raise ValueError(f"{field}.{min(unexpected)} is not supported")
        finding_id = _review_text(raw.get("id"), f"{field}.id")
        _safe_segment(finding_id, f"{field}.id")
        if finding_id in finding_ids:
            raise ValueError(f"{field}.id duplicates {finding_id!r}")
        finding_ids.add(finding_id)
        code = _review_text(raw.get("code"), f"{field}.code")
        if not _FINDING_CODE_RE.fullmatch(code):
            raise ValueError(f"{field}.code must be kebab-case")
        category = _review_enum(raw.get("category"), f"{field}.category", _REVIEW_CATEGORIES)
        severity = _review_enum(raw.get("severity"), f"{field}.severity", _REVIEW_SEVERITIES)
        subject = raw.get("subject")
        if not isinstance(subject, dict):
            raise TypeError(f"{field}.subject must be an object")
        if set(subject) != {"kind", "id"}:
            raise ValueError(f"{field}.subject must contain only kind and id")
        subject_kind = _review_enum(
            subject.get("kind"), f"{field}.subject.kind", _REVIEW_SUBJECT_KINDS
        )
        subject_id = _review_text(subject.get("id"), f"{field}.subject.id")
        if subject_kind == "plan":
            if parse_plan_ref(subject_id) is None:
                raise ValueError(f"{field}.subject.id is not a valid plan ref")
        else:
            try:
                _safe_segment(subject_id, f"{field}.subject.id")
            except ValueError as exc:
                raise ValueError(f"{field}.subject.id is malformed") from exc
        evidence = raw.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"{field}.evidence must be a non-empty list")
        evidence = [
            _review_text(line, f"{field}.evidence[{line_index}]")
            for line_index, line in enumerate(evidence)
        ]
        action = raw.get("recommended_action")
        if not isinstance(action, dict):
            raise TypeError(f"{field}.recommended_action must be an object")
        if set(action) != {"verb", "owner_skill", "detail"}:
            raise ValueError(
                f"{field}.recommended_action must contain only verb, owner_skill, and detail"
            )
        verb = _review_enum(
            action.get("verb"), f"{field}.recommended_action.verb", _REVIEW_ACTIONS
        )
        owner_skill = _review_text(
            action.get("owner_skill"), f"{field}.recommended_action.owner_skill"
        )
        detail = _review_text(
            action.get("detail"), f"{field}.recommended_action.detail"
        )
        validation = _review_enum(
            raw.get("validated"), f"{field}.validated", _REVIEW_VALIDATIONS
        )
        checked_at = _review_date(raw.get("checked_at"), f"{field}.checked_at")
        resolved_at = raw.get("resolved_at") or ""
        resolved_by = raw.get("resolved_by") or ""
        outcome = raw.get("outcome") or ""
        if resolved_at:
            try:
                datetime.fromisoformat(str(resolved_at))
            except ValueError as exc:
                raise ValueError(f"{field}.resolved_at must be an ISO date or datetime") from exc
            resolved_by = _review_text(resolved_by, f"{field}.resolved_by")
            outcome = _review_text(outcome, f"{field}.outcome")
        elif resolved_by or outcome:
            raise ValueError(f"{field}.resolved_at is required for resolution fields")
        validated_findings.append(
            {
                "id": finding_id,
                "code": code,
                "category": category,
                "severity": severity,
                "subject": {"kind": subject_kind, "id": subject_id},
                "evidence": evidence,
                "recommended_action": {
                    "verb": verb,
                    "owner_skill": owner_skill,
                    "detail": detail,
                },
                "validated": validation,
                "checked_at": checked_at,
                "resolved_at": str(resolved_at),
                "resolved_by": str(resolved_by),
                "outcome": str(outcome),
            }
        )
    priority = data.get("priority", [])
    if not isinstance(priority, list):
        raise TypeError("priority must be a list")
    validated_priority: list[dict[str, Any]] = []
    priority_refs: set[str] = set()
    for index, raw in enumerate(priority):
        field = f"priority[{index}]"
        if not isinstance(raw, dict):
            raise TypeError(f"{field} must be an object")
        if set(raw) != {"rank", "ref", "reasons", "detail"}:
            raise ValueError(f"{field} must contain only rank, ref, reasons, and detail")
        rank = raw.get("rank")
        if type(rank) is not int or rank != index + 1:
            raise ValueError(f"{field}.rank must be contiguous from 1")
        ref = _review_text(raw.get("ref"), f"{field}.ref")
        if parse_plan_ref(ref) is None:
            raise ValueError(f"{field}.ref is not a valid plan ref")
        if ref in priority_refs:
            raise ValueError(f"{field}.ref duplicates {ref!r}")
        priority_refs.add(ref)
        reasons = raw.get("reasons")
        if not isinstance(reasons, list) or not reasons:
            raise ValueError(f"{field}.reasons must be a non-empty list")
        if len(reasons) != len(set(reasons)):
            raise ValueError(f"{field}.reasons must be unique")
        reasons = [
            _review_enum(reason, f"{field}.reasons[{reason_index}]", _PRIORITY_REASONS)
            for reason_index, reason in enumerate(reasons)
        ]
        validated_priority.append(
            {
                "rank": rank,
                "ref": ref,
                "reasons": reasons,
                "detail": _review_text(raw.get("detail"), f"{field}.detail"),
            }
        )
    return {
        "id": "review",
        "type": "review",
        "version": int(data.get("version", 0) or 0),
        "reviewed_at": reviewed_at,
        "reviewed_by": reviewed_by,
        "basis": basis,
        "findings": validated_findings,
        "priority": validated_priority,
    }


def _validate_resource(resource_type: str, data: dict[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(data)
    if resource_type == "sprint":
        sprint = Sprint.model_validate(cleaned)
        cleaned = sprint.model_dump(
            exclude_none=True, exclude_unset=True, by_alias=True
        )
        cleaned["id"] = data.get("id", sprint.id)
        cleaned["version"] = int(data.get("version", 0) or 0)
        _safe_segment(str(cleaned.get("id", "")), "sprint id")
        if cleaned.get("status", "planned") not in SPRINT_STATUS_ENUM:
            raise ValueError(f"sprint status must be one of {SPRINT_STATUS_ENUM}")
        seen_slugs: set[str] = set()
        for item in cleaned.get("items", []):
            slug = str(item.get("slug", ""))
            _safe_segment(slug, "sprint item slug")
            if slug in seen_slugs:
                raise ValueError(f"duplicate sprint item slug {slug!r}")
            seen_slugs.add(slug)
            for field in LIFECYCLE_ITEM_FIELDS:
                if field in item:
                    raise ValueError(f"sprint item {slug!r} must not persist {field}")
            blockers = item.get("blocked_by", [])
            if not isinstance(blockers, list):
                raise ValueError(f"sprint item {slug!r} blocked_by must be a list")
            if len(blockers) != len(set(blockers)):
                raise ValueError(
                    f"sprint item {slug!r} has duplicate blocker references"
                )
            for blocker_id in blockers:
                _safe_segment(str(blocker_id), "blocked_by id")
            milestone_id = item.get("milestone")
            if milestone_id:
                _safe_segment(str(milestone_id), "sprint item milestone")
    elif resource_type == "milestone":
        cleaned = Milestone.model_validate(cleaned).model_dump(
            exclude_none=True, exclude_unset=True
        )
        cleaned["version"] = int(data.get("version", 0) or 0)
        dependencies = cleaned.get("depends_on") or []
        if not isinstance(dependencies, list):
            raise ValueError("milestone depends_on must be a list")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("milestone dependencies must be unique")
        for dependency in dependencies:
            _safe_segment(str(dependency), "milestone dependency")
    elif resource_type == "blocker":
        cleaned = Blocker.model_validate(cleaned).model_dump(
            exclude_none=True, exclude_unset=True
        )
        cleaned.pop("n", None)
        cleaned["version"] = int(data.get("version", 0) or 0)
    elif resource_type == "timeline":
        events = cleaned.get("events", [])
        if not isinstance(events, list):
            raise ValueError("timeline events must be a list")
        validated = []
        ids: set[str] = set()
        for event in events:
            item = TimelineEntry.model_validate(event).model_dump(exclude_none=True)
            item.update({k: v for k, v in event.items() if k not in item})
            ident = str(item.get("id", ""))
            if not ident or ident in ids:
                raise ValueError("timeline event ids must be non-empty and unique")
            _safe_segment(ident, "timeline event id")
            ids.add(ident)
            validated.append(item)
        cleaned = {
            "id": "timeline",
            "type": "timeline",
            "version": int(data.get("version", 0) or 0),
            "events": validated,
        }
    elif resource_type == "review":
        cleaned = _validate_review(cleaned)
    elif resource_type == "project":
        bad = PROJECT_DERIVED_FIELDS & set(cleaned) - {"type", "id", "version"}
        if bad:
            raise ValueError(
                "project manifest is identity/presentation-only; forbidden keys: "
                + ", ".join(sorted(bad))
            )
        scope = cleaned.get("scope")
        if scope is not None:
            if not isinstance(scope, dict):
                raise ValueError("project scope must be an object")
            for field in ("owns", "excludes"):
                values = scope.get(field, [])
                if not isinstance(values, list) or any(
                    not isinstance(value, str) or not value.strip() for value in values
                ):
                    raise ValueError(f"project scope {field} must be a string list")
            routes = scope.get("routes", [])
            if not isinstance(routes, list):
                raise ValueError("project scope routes must be a list")
            for route in routes:
                if not isinstance(route, dict):
                    raise ValueError("project scope route must be an object")
                work = route.get("work")
                destination = route.get("project")
                if not isinstance(work, str) or not work.strip():
                    raise ValueError("project scope route work must be non-empty")
                _safe_segment(str(destination or ""), "project scope route project")
        derivations = cleaned.get("derivations")
        if derivations is not None:
            if not isinstance(derivations, dict):
                raise ValueError("project derivations must be an object")
            validated_derivations: dict[str, list[str]] = {}
            for source, generated in derivations.items():
                source_path = _repository_relative_path(
                    source, "project derivation source"
                )
                if not isinstance(generated, list):
                    raise ValueError(
                        f"project derivation outputs for {source!r} must be a list"
                    )
                validated_derivations[source_path] = [
                    _repository_relative_path(
                        generated_path, "project derivation generated path"
                    )
                    for generated_path in generated
                ]
            cleaned["derivations"] = validated_derivations
        publication = cleaned.get("publication")
        if publication is not None:
            if not isinstance(publication, dict):
                raise ValueError("project publication must be an object")
            unexpected = set(publication) - {"enabled"}
            if unexpected:
                raise ValueError(
                    "project publication has unsupported keys: "
                    + ", ".join(sorted(unexpected))
                )
            if not isinstance(publication.get("enabled"), bool):
                raise ValueError("project publication enabled must be a boolean")
        north_stars = cleaned.get("north_stars")
        if north_stars is not None:
            if not isinstance(north_stars, list):
                raise ValueError("project north_stars must be a list")
            validated_north_stars: list[dict[str, Any]] = []
            seen_north_star_ids: set[str] = set()
            for raw in north_stars:
                north_star = NorthStar.model_validate(raw).model_dump(exclude_none=True)
                for field in ("id", "name", "statement"):
                    if not north_star[field].strip():
                        raise ValueError(f"north-star {field} must be non-empty")
                north_star_id = north_star["id"]
                _safe_segment(north_star_id, "north-star id")
                if north_star_id in seen_north_star_ids:
                    raise ValueError(f"duplicate north-star id {north_star_id!r}")
                seen_north_star_ids.add(north_star_id)
                validated_north_stars.append(north_star)
            cleaned["north_stars"] = validated_north_stars
        cleaned["version"] = int(data.get("version", 0) or 0)
    cleaned["type"] = resource_type
    return cleaned


def _live_plan_slugs(docs_dir: Path, project: str) -> set[str]:
    from reckon.resources import resource_map

    return {
        resource.slug
        for resource in resource_map(
            docs_dir,
            project,
            include_archived=False,
            ignore_invalid=True,
        ).values()
        if resource.type == "plan"
    }


def _distributed_ids(docs_dir: Path, root_name: str) -> set[str]:
    root = docs_dir / root_name
    return {path.stem for path in root.glob("*.html")} if root.is_dir() else set()


def _validate_runtime_references(
    docs_dir: Path,
    project: str,
    resource_type: str,
    resource_id: str,
    data: dict[str, Any],
) -> None:
    """Validate references against the complete installed distributed corpus."""
    if resource_type == "sprint":
        plan_slugs = _live_plan_slugs(docs_dir, project)
        blocker_ids = _distributed_ids(docs_dir, "blockers")
        milestone_ids = _distributed_ids(docs_dir, "milestones")
        for item in data.get("items", []):
            slug = str(item["slug"])
            if slug not in plan_slugs:
                raise ValueError(
                    f"sprint item {slug!r} does not resolve to a live plan"
                )
            missing = set(item.get("blocked_by", [])) - blocker_ids
            if missing:
                raise ValueError(
                    f"sprint item {slug!r} references missing blockers: "
                    + ", ".join(sorted(missing))
                )
            milestone_id = item.get("milestone")
            if milestone_id and milestone_id not in milestone_ids:
                raise ValueError(
                    f"sprint item {slug!r} references missing milestone "
                    f"{milestone_id!r}"
                )
    elif resource_type == "milestone":
        milestone_ids = _distributed_ids(docs_dir, "milestones") | {resource_id}
        missing = set(data.get("depends_on") or []) - milestone_ids
        if missing:
            raise ValueError(
                f"milestone {resource_id!r} references missing milestones: "
                + ", ".join(sorted(missing))
            )


def write_resource(
    docs_dir: Path,
    project: str,
    resource_type: str,
    resource_id: str,
    data: dict[str, Any],
    expected_version: int,
    *,
    create: bool = False,
) -> int:
    """Version-check and atomically write one distributed resource."""
    mode = project_state_mode(docs_dir)
    if mode.format != "distributed":
        raise ProjectStateError(
            "distributed_resource_inactive: a complete distributed marker is "
            "required before distributed writes"
        )
    with _resource_lock(docs_dir, project, resource_type, resource_id):
        return _write_resource_unlocked(
            docs_dir,
            project,
            resource_type,
            resource_id,
            data,
            expected_version,
            create=create,
        )


def enable_project_publication(docs_dir: Path, project: str) -> tuple[int, bool]:
    """Persist an explicit publication opt-in without advancing unchanged state."""
    manifest, version = read_resource(docs_dir, project, "project", "project")
    publication = manifest.get("publication")
    if isinstance(publication, dict) and publication.get("enabled") is True:
        return version, False
    new_version = write_resource(
        docs_dir,
        project,
        "project",
        "project",
        {**manifest, "publication": {"enabled": True}},
        version,
    )
    return new_version, True


def _write_resource_unlocked(
    docs_dir: Path,
    project: str,
    resource_type: str,
    resource_id: str,
    data: dict[str, Any],
    expected_version: int,
    *,
    create: bool = False,
) -> int:
    """Write while the caller holds the corresponding resource lock."""
    path = resource_path(docs_dir, project, resource_type, resource_id)
    if path.exists():
        current, current_version = _read_resource_unchecked(
            docs_dir, project, resource_type, resource_id
        )
        if create:
            raise ValueError(f"{resource_type} {resource_id!r} already exists")
    else:
        current, current_version = {}, 0
        if not create:
            raise FileNotFoundError(f"{resource_type} {resource_id!r} does not exist")
    if expected_version != current_version:
        raise ProjectStateConflict(expected_version, current_version, current)
    payload = _validate_resource(
        resource_type,
        {
            **data,
            "id": resource_id,
            "type": resource_type,
            "version": current_version + 1,
        },
    )
    if resource_type == "timeline" and current:
        previous_events = list(current.get("events", []))
        next_events = list(payload.get("events", []))
        if (
            len(next_events) < len(previous_events)
            or next_events[: len(previous_events)] != previous_events
        ):
            raise ValueError(
                "timeline is append-only; existing events must remain an exact prefix"
            )
    _validate_runtime_references(
        docs_dir,
        project,
        resource_type,
        resource_id,
        payload,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    if resource_type == "project":
        envelope = {
            "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            "project": project,
            "doc": "project",
            "data": payload,
        }
        tmp.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    else:
        tmp.write_text(
            _render_resource(project, resource_type, resource_id, payload),
            encoding="utf-8",
        )
    try:
        _durable_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return current_version + 1


def _load_legacy_index(docs_dir: Path, project: str) -> tuple[dict[str, Any], bytes]:
    path = legacy_index_path(docs_dir, project)
    if not path.is_file():
        return {}, b""
    raw = path.read_bytes()
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectStateError(f"legacy index is malformed: {path}") from exc
    data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
    if not isinstance(data, dict):
        raise ProjectStateError(f"legacy index data is malformed: {path}")
    return data, raw


def _event_id(event: dict[str, Any], position: int) -> str:
    digest = _sha256_bytes(_canonical_json({"position": position, "event": event}))[:16]
    return f"event-{digest}"


def _apply_migration_composed_derivations(
    data: dict[str, Any],
) -> dict[str, Any]:
    """Apply deterministic field synthesis declared by historical receipts."""
    derived = deepcopy(data)
    for position, blocker in enumerate(derived.get("blockers", [])):
        if "blockers[].id" in MIGRATION_COMPOSED_DERIVATIONS and isinstance(
            blocker, dict
        ):
            blocker["id"] = str(blocker.get("id") or f"blocker-{position + 1}")
        if "blockers[].next" in MIGRATION_COMPOSED_DERIVATIONS and isinstance(
            blocker, dict
        ):
            blocker.setdefault("next", "")
    for position, event in enumerate(derived.get("timeline", [])):
        if "timeline[].id" in MIGRATION_COMPOSED_DERIVATIONS and isinstance(
            event, dict
        ):
            event["id"] = str(event.get("id") or _event_id(event, position))
    return derived


def _identity_manifest(project: str, legacy: dict[str, Any]) -> dict[str, Any]:
    project_rows = legacy.get("projects") or []
    row = (
        dict(project_rows[0])
        if project_rows and isinstance(project_rows[0], dict)
        else {}
    )
    retained = {
        key: deepcopy(value)
        for key, value in row.items()
        if key not in PROJECT_DERIVED_FIELDS
    }
    if "north_stars" in legacy:
        retained["north_stars"] = deepcopy(legacy["north_stars"])
    return {**retained, "project": project, "version": 0}


def _plan_state_by_slug(docs_dir: Path, project: str) -> dict[str, dict[str, Any]]:
    from reckon import _plan_html
    from reckon.resources import resource_map

    result: dict[str, dict[str, Any]] = {}
    for resource in resource_map(
        docs_dir,
        project,
        include_archived=False,
        ignore_invalid=True,
    ).values():
        if resource.type != "plan":
            continue
        result[resource.slug] = _plan_html.read_state(
            resource.path.read_text(encoding="utf-8", errors="replace")
        )
    return result


def _derive_default_sprint_focus(sprints: list[dict[str, Any]]) -> str | None:
    """Return the first sprint in composed order with unfinished plan work."""
    for sprint in sprints:
        for item in sprint.get("items", []):
            status = item.get("status") if isinstance(item, dict) else None
            if str(status or "") not in COMPLETED_STATUSES:
                return str(sprint.get("id") or "") or None
    return None


def _derive_blocker_counts(
    blockers: list[dict[str, Any]], sprints: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    gated_plans: dict[str, set[str]] = {}
    for sprint in sprints:
        for item in sprint.get("items", []):
            if not isinstance(item, dict):
                continue
            for blocker_id in item.get("blocked_by", []):
                key = str(blocker_id)
                counts[key] = counts.get(key, 0) + 1
                slug = str(item.get("slug") or "")
                if slug:
                    gated_plans.setdefault(key, set()).add(slug)
    return [
        {
            **item,
            "n": counts.get(str(item.get("id")), 0),
            "gated_plans": sorted(gated_plans.get(str(item.get("id")), set())),
        }
        for item in blockers
    ]


def _hydrate_items(
    docs_dir: Path, project: str, sprints: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    plans = _plan_state_by_slug(docs_dir, project)
    hydrated = deepcopy(sprints)
    for sprint in hydrated:
        items: list[Any] = []
        for raw in sprint.get("items", []):
            item = {"slug": raw} if isinstance(raw, str) else dict(raw)
            state = plans.get(str(item.get("slug", "")))
            if state:
                for key in (
                    "title",
                    "status",
                    "impl",
                    "effort_hours",
                    "effort_calibrated",
                ):
                    if key in state:
                        item[key] = state[key]
            items.append(item)
        sprint["items"] = items
        sprint["capacity"] = {
            "total_hours": round(
                sum(
                    float(item.get("effort_hours") or 0.0)
                    for item in items
                    if isinstance(item, dict)
                ),
                2,
            ),
            "unit": "worker-hours",
        }
    return hydrated


def _item_lifecycle_warnings(sprints: list[dict[str, Any]]) -> list[str]:
    """Report stored item fields whose values are supplied by live plans."""
    warnings: list[str] = []
    for sprint in sprints:
        sprint_id = str(sprint.get("id") or "<no-id>")
        for raw in sprint.get("items", []):
            if not isinstance(raw, dict):
                continue
            slug = str(raw.get("slug") or "<no-slug>")
            for field in sorted(LIFECYCLE_ITEM_FIELDS & raw.keys()):
                warnings.append(
                    f"sprint {sprint_id} item {slug}: persisted {field} is ignored; "
                    "the value is derived from plan HTML"
                )
    return warnings


def _natural_identifier_key(value: Any) -> tuple[tuple[int, Any], ...]:
    """Order identifiers by text fragments and the value of numeric fragments."""
    return tuple(
        (0, int(fragment)) if fragment.isdigit() else (1, fragment.casefold())
        for fragment in re.split(r"(\d+)", str(value))
        if fragment
    )


def _order_distributed_sprints(
    sprints: list[dict[str, Any]], resources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Use a marker's authored sequence when distinguishable, else natural order."""
    marker_ids = [
        str(row.get("id", ""))
        for row in resources
        if row.get("type") == "sprint"
    ]
    marker_is_lexicographic = marker_ids == sorted(marker_ids)
    if marker_ids and not marker_is_lexicographic:
        positions = {
            resource_id: position for position, resource_id in enumerate(marker_ids)
        }
        return sorted(
            sprints,
            key=lambda item: (
                0,
                positions[str(item.get("id", ""))],
            )
            if str(item.get("id", "")) in positions
            else (1, _natural_identifier_key(item.get("id", ""))),
        )
    return sorted(
        sprints, key=lambda item: _natural_identifier_key(item.get("id", ""))
    )


def compose_project_state(docs_dir: Path, project: str) -> dict[str, Any]:
    """Compose the distributed resources into the compatibility index shape."""
    mode = project_state_mode(docs_dir)
    if mode.format != "distributed":
        data, _ = _load_legacy_index(docs_dir, project)
        composed = deepcopy(data)
        sprints = list(composed.get("sprints", []))
        warnings = _item_lifecycle_warnings(sprints)
        composed["sprints"] = _hydrate_items(docs_dir, project, sprints)
        if warnings:
            composed["compatibility_warnings"] = [
                *composed.get("compatibility_warnings", []),
                *warnings,
            ]
        return {**composed, "source_format": "legacy-index"}

    marker = mode.marker or {}
    resources = marker.get("resources")
    if not isinstance(resources, list):
        raise ProjectStateError("distributed marker has no resource inventory")
    sprints: list[dict[str, Any]] = []
    milestones: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    project_manifest: dict[str, Any] = {}
    versions: dict[str, int] = {}
    resource_keys: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for row in resources:
        if not isinstance(row, dict):
            raise ProjectStateError("distributed marker resource row is malformed")
        resource_type = str(row.get("type", ""))
        resource_id = str(row.get("id", ""))
        key_tuple = (resource_type, resource_id)
        if key_tuple in seen_keys:
            raise ProjectStateError(
                f"duplicate distributed resource identity: {key_tuple}"
            )
        seen_keys.add(key_tuple)
        resource_keys.append(key_tuple)
    for resource_type, root_name in (
        ("sprint", "sprints"),
        ("milestone", "milestones"),
        ("blocker", "blockers"),
    ):
        root = docs_dir / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.html")):
            key_tuple = (resource_type, path.stem)
            if key_tuple not in seen_keys:
                seen_keys.add(key_tuple)
                resource_keys.append(key_tuple)

    for resource_type, resource_id in resource_keys:
        data, version = read_resource(docs_dir, project, resource_type, resource_id)
        key = f"{resource_type}:{resource_id}"
        versions[key] = version
        if resource_type == "sprint":
            sprints.append(data)
        elif resource_type == "milestone":
            milestones.append(data)
        elif resource_type == "blocker":
            blockers.append(data)
        elif resource_type == "timeline":
            timeline = list(data.get("events", []))
        elif resource_type == "project":
            project_manifest = data
    sprints = _order_distributed_sprints(sprints, resources)
    milestones.sort(key=lambda item: str(item.get("id", "")))
    blockers.sort(key=lambda item: str(item.get("id", "")))
    compatibility_warnings = [
        warning
        for sprint in sprints
        for warning in sprint.get("compatibility_warnings", [])
    ]
    hydrated_sprints = _hydrate_items(docs_dir, project, sprints)
    focus_id = _derive_default_sprint_focus(hydrated_sprints)
    composed = {
        "_version": 0,
        "active_sprint_id": focus_id,
        "projects": [project_manifest],
        "sprints": hydrated_sprints,
        "milestones": milestones,
        "blockers": _derive_blocker_counts(blockers, hydrated_sprints),
        "timeline": timeline,
        "source_format": "distributed",
        "resource_versions": versions,
    }
    if "north_stars" in project_manifest:
        composed["north_stars"] = deepcopy(project_manifest["north_stars"])
    if compatibility_warnings:
        composed["compatibility_warnings"] = compatibility_warnings
    return composed


def audit_project_state(docs_dir: Path, project: str) -> list[dict[str, str]]:
    """Report invalid distributed project resources."""
    if project_state_mode(docs_dir).format != "distributed":
        return []
    try:
        composed = compose_project_state(docs_dir, project)
    except ProjectStateError as exc:
        message = str(exc)
        return [
            {
                "code": "distributed-project-state-invalid",
                "severity": "error",
                "message": message,
            }
        ]
    north_star_count = len(composed.get("north_stars") or [])
    if north_star_count > NORTH_STAR_ADVISORY_CAP:
        return [
            {
                "code": "north-star-advisory-cap-exceeded",
                "severity": "warning",
                "message": (
                    f"project declares {north_star_count} north-stars; "
                    f"the advisory cap is {NORTH_STAR_ADVISORY_CAP}"
                ),
            }
        ]
    return []


def _parity_projection(
    data: dict[str, Any], project: str | None = None
) -> dict[str, Any]:
    """Normalise old/new views to durable, non-derived project state."""
    data = _apply_migration_composed_derivations(data)
    result = {
        "project": {},
        "sprints": [],
        "milestones": [],
        "blockers": [],
        "timeline": [],
    }
    for sprint in data.get("sprints", []):
        record = deepcopy(sprint)
        record.pop("type", None)
        record.pop("resource_id", None)
        record.pop("href", None)
        record.pop("version", None)
        record.pop("capacity", None)
        for field in ("description", "starts", "ends", "summary"):
            if not record.get(field):
                record.pop(field, None)
        items = []
        for raw_item in record.get("items", []):
            item = {"slug": raw_item} if isinstance(raw_item, str) else raw_item
            if isinstance(item, dict):
                for field in (
                    *LIFECYCLE_ITEM_FIELDS,
                    "title",
                    "effort_hours",
                    "effort_calibrated",
                ):
                    item.pop(field, None)
            items.append(item)
        record["items"] = items
        result["sprints"].append(record)
    for key in ("milestones", "blockers"):
        for row in data.get(key, []):
            record = deepcopy(row)
            record.pop("type", None)
            record.pop("version", None)
            if key == "blockers":
                record.pop("n", None)
                if not record.get("origin"):
                    record.pop("origin", None)
            result[key].append(record)
    for row in data.get("timeline", []):
        record = deepcopy(row)
        result["timeline"].append(record)
    project_rows = data.get("projects") or []
    project_row = (
        project_rows[0]
        if project_rows and isinstance(project_rows[0], dict)
        else data
        if data.get("type") == "project"
        else {}
    )
    result["project"] = {
        key: deepcopy(value)
        for key, value in project_row.items()
        if key not in PROJECT_DERIVED_FIELDS
    }
    for key in ("project", "owner", "published"):
        result["project"].setdefault(key, "")
    if project and not result["project"]["project"]:
        result["project"]["project"] = project
    for key in ("sprints", "milestones", "blockers"):
        result[key] = sorted(result[key], key=lambda item: _canonical_json(item))
    return result


def _write_staged_resource(
    staging_docs: Path,
    project: str,
    resource_type: str,
    resource_id: str,
    data: dict[str, Any],
) -> Path:
    path = resource_path(staging_docs, project, resource_type, resource_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _validate_resource(resource_type, data)
    if resource_type == "project":
        envelope = {
            "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            "project": project,
            "doc": "project",
            "data": payload,
        }
        path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(
            _render_resource(project, resource_type, resource_id, payload),
            encoding="utf-8",
        )
    return path


def create_project_state(docs_dir: Path, project: str) -> dict[str, Any]:
    """Create an empty distributed project-state corpus as one transaction.

    The complete marker is published only after every typed resource is
    installed. Existing legacy state is never converted by this entrypoint.
    """
    docs_dir = docs_dir.resolve()
    _safe_segment(project, "project")
    with _resource_lock(docs_dir, project, "creation", "project-state"):
        mode = project_state_mode(docs_dir)
        if mode.format == "distributed":
            compose_project_state(docs_dir, project)
            return {"ok": True, "changed": False, **(mode.marker or {})}
        if marker_path(docs_dir).exists():
            raise ProjectStateError(
                "project-state creation requires no existing marker"
            )
        if legacy_index_path(docs_dir, project).exists():
            raise ProjectStateError(
                "project-state creation refuses an existing legacy index"
            )

        payloads = {
            ("project", "project"): _identity_manifest(project, {}),
            ("timeline", "timeline"): {
                "id": "timeline",
                "type": "timeline",
                "version": 0,
                "events": [],
            },
        }
        stage_root = Path(
            tempfile.mkdtemp(
                prefix="project-state-create-", dir=docs_dir / ".reckon"
            )
        )
        staging_docs = stage_root / "docs"
        staged: list[tuple[str, str, Path]] = []
        installed: list[Path] = []
        try:
            for (resource_type, resource_id), payload in sorted(payloads.items()):
                staged_path = _write_staged_resource(
                    staging_docs, project, resource_type, resource_id, payload
                )
                _read_resource_unchecked(
                    staging_docs, project, resource_type, resource_id
                )
                staged.append((resource_type, resource_id, staged_path))

            destinations = [
                resource_path(docs_dir, project, resource_type, resource_id)
                for resource_type, resource_id, _ in staged
            ]
            collisions = [path for path in destinations if path.exists()]
            if collisions:
                raise ProjectStateError(
                    "project-state creation refuses existing typed resources: "
                    + ", ".join(str(path.relative_to(docs_dir)) for path in collisions)
                )

            for (_, _, source), destination in zip(staged, destinations, strict=True):
                destination.parent.mkdir(parents=True, exist_ok=True)
                _durable_replace(source, destination)
                installed.append(destination)

            rows = [
                {
                    "type": resource_type,
                    "id": resource_id,
                    "path": str(destination.relative_to(docs_dir)),
                    "sha256": _sha256_path(destination),
                    "version": _read_resource_unchecked(
                        docs_dir, project, resource_type, resource_id
                    )[1],
                }
                for (resource_type, resource_id, _), destination in zip(
                    staged, destinations, strict=True
                )
            ]
            marker = {
                "format": "distributed",
                "status": "complete",
                "project": project,
                "resources": rows,
                "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            target = marker_path(docs_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, marker_tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            os.close(fd)
            marker_tmp = Path(marker_tmp_name)
            try:
                marker_tmp.write_text(
                    json.dumps(marker, indent=2) + "\n", encoding="utf-8"
                )
                _durable_replace(marker_tmp, target)
            finally:
                marker_tmp.unlink(missing_ok=True)
            return {"ok": True, "changed": True, **marker}
        except BaseException:
            _durable_unlink(marker_path(docs_dir))
            for destination in reversed(installed):
                _durable_unlink(destination)
            raise
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)


def append_timeline_event(
    docs_dir: Path,
    project: str,
    event: dict[str, Any],
    expected_version: int,
) -> int:
    """Append one event without permitting mutation of existing history."""
    data, version = read_resource(docs_dir, project, "timeline", "timeline")
    if version != expected_version:
        raise ProjectStateConflict(expected_version, version, data)
    events = list(data.get("events", []))
    item = dict(event)
    if not item.get("id"):
        item["id"] = _event_id(item, len(events))
    if any(existing.get("id") == item["id"] for existing in events):
        raise ValueError(f"timeline event id {item['id']!r} already exists")
    return write_resource(
        docs_dir,
        project,
        "timeline",
        "timeline",
        {**data, "events": [*events, item]},
        expected_version,
    )


def _move_journal_path(
    docs_dir: Path, project: str, from_sprint: str, to_sprint: str, slug: str
) -> Path:
    digest = _sha256_bytes(
        _canonical_json(
            {
                "project": project,
                "from": from_sprint,
                "to": to_sprint,
                "slug": slug,
            }
        )
    )[:16]
    return docs_dir / ".reckon" / "transactions" / f"sprint-move-{digest}.json"


def _publish_move_journal(
    path: Path,
    project: str,
    source_id: str,
    target_id: str,
    source_bytes: bytes,
    target_bytes: bytes,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "sprint-item-move",
        "status": "prepared",
        "project": project,
        "source_id": source_id,
        "target_id": target_id,
        "source_before": base64.b64encode(source_bytes).decode("ascii"),
        "target_before": base64.b64encode(target_bytes).decode("ascii"),
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _durable_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _mark_move_journal_committed(path: Path) -> None:
    """Durably publish commit completion before removing recovery evidence."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "committed"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _durable_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def recover_project_state_transactions(docs_dir: Path, project: str) -> list[Path]:
    """Restore both sides of any prepared sprint-move journal."""
    recovered: list[Path] = []
    root = docs_dir / ".reckon" / "transactions"
    if not root.is_dir():
        return recovered
    with _resource_lock(docs_dir, project, "transactions", "recovery"):
        for journal in sorted(root.glob("sprint-move-*.json")):
            first_bytes = journal.read_bytes()
            try:
                payload = json.loads(first_bytes)
                if (
                    payload.get("kind") != "sprint-item-move"
                    or payload.get("project") != project
                ):
                    raise ProjectStateError(f"malformed transaction journal: {journal}")
                source_id = _safe_segment(
                    str(payload["source_id"]), "journal source id"
                )
                target_id = _safe_segment(
                    str(payload["target_id"]), "journal target id"
                )
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                raise ProjectStateError(
                    f"malformed transaction journal: {journal}"
                ) from exc
            with _resource_locks(
                docs_dir,
                project,
                [("sprint", source_id), ("sprint", target_id)],
            ):
                if not journal.exists():
                    continue
                locked_bytes = journal.read_bytes()
                if locked_bytes != first_bytes:
                    # Another operation changed the journal before resource
                    # locks were held. Re-evaluate it on the next recovery pass.
                    continue
                payload = json.loads(locked_bytes)
                status = payload.get("status")
                if status == "committed":
                    _durable_unlink(journal)
                    recovered.append(journal)
                    continue
                if status != "prepared":
                    raise ProjectStateError(f"malformed transaction journal: {journal}")
                try:
                    source_bytes = base64.b64decode(
                        payload["source_before"], validate=True
                    )
                    target_bytes = base64.b64decode(
                        payload["target_before"], validate=True
                    )
                except (KeyError, ValueError) as exc:
                    raise ProjectStateError(
                        f"malformed transaction journal: {journal}"
                    ) from exc
                source_path = resource_path(docs_dir, project, "sprint", source_id)
                target_path = resource_path(docs_dir, project, "sprint", target_id)
                for path, content in (
                    (source_path, source_bytes),
                    (target_path, target_bytes),
                ):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    fd, tmp_name = tempfile.mkstemp(
                        prefix=f".{path.name}.",
                        suffix=".recover",
                        dir=path.parent,
                    )
                    os.close(fd)
                    tmp = Path(tmp_name)
                    try:
                        tmp.write_bytes(content)
                        _durable_replace(tmp, path)
                    finally:
                        tmp.unlink(missing_ok=True)
                _durable_unlink(journal)
                recovered.append(journal)
    return recovered


def move_sprint_item(
    docs_dir: Path,
    project: str,
    slug: str,
    from_sprint: str,
    to_sprint: str,
    expected_from_version: int,
    expected_to_version: int,
    *,
    after_first_write: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Move one item with two-version validation and compensating recovery."""
    source_path = resource_path(docs_dir, project, "sprint", from_sprint)
    target_path = resource_path(docs_dir, project, "sprint", to_sprint)
    journal = _move_journal_path(docs_dir, project, from_sprint, to_sprint, slug)
    with _resource_locks(
        docs_dir,
        project,
        [("sprint", from_sprint), ("sprint", to_sprint)],
    ):
        source, source_version = _read_resource_unchecked(
            docs_dir, project, "sprint", from_sprint
        )
        target, target_version = _read_resource_unchecked(
            docs_dir, project, "sprint", to_sprint
        )
        if source_version != expected_from_version:
            raise ProjectStateConflict(expected_from_version, source_version, source)
        if target_version != expected_to_version:
            raise ProjectStateConflict(expected_to_version, target_version, target)
        source_items = list(source.get("items", []))
        target_items = list(target.get("items", []))
        source_index = next(
            (
                i
                for i, item in enumerate(source_items)
                if (item if isinstance(item, str) else item.get("slug")) == slug
            ),
            None,
        )
        if source_index is None:
            raise ValueError(f"{slug!r} not found in sprint {from_sprint}")
        if any(
            (item if isinstance(item, str) else item.get("slug")) == slug
            for item in target_items
        ):
            raise ValueError(f"{slug!r} already exists in sprint {to_sprint}")
        moved = source_items.pop(source_index)
        target_items.append(moved)
        source_bytes = source_path.read_bytes()
        target_bytes = target_path.read_bytes()
        _publish_move_journal(
            journal,
            project,
            from_sprint,
            to_sprint,
            source_bytes,
            target_bytes,
        )
        new_from = _write_resource_unlocked(
            docs_dir,
            project,
            "sprint",
            from_sprint,
            {**source, "items": source_items},
            source_version,
        )
        try:
            if after_first_write:
                after_first_write()
            new_to = _write_resource_unlocked(
                docs_dir,
                project,
                "sprint",
                to_sprint,
                {**target, "items": target_items},
                target_version,
            )
        except Exception:
            for path, content in (
                (source_path, source_bytes),
                (target_path, target_bytes),
            ):
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".recover", dir=path.parent
                )
                os.close(fd)
                tmp = Path(tmp_name)
                try:
                    tmp.write_bytes(content)
                    _durable_replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
            _durable_unlink(journal)
            raise
        _mark_move_journal_committed(journal)
        _durable_unlink(journal)
        return {"from_version": new_from, "to_version": new_to}


def apply_resource_ops(
    docs_dir: Path,
    project: str,
    resource_type: str,
    resource_id: str,
    ops: list[dict[str, Any]],
    expected_version: int,
    *,
    create: bool = False,
) -> tuple[int, list[str]]:
    """Apply the collapsed edit vocabulary to one distributed resource."""
    if create:
        working: dict[str, Any] = {"id": resource_id, "type": resource_type}
    else:
        working, current = read_resource(docs_dir, project, resource_type, resource_id)
        if current != expected_version:
            raise ProjectStateConflict(expected_version, current, working)
    warnings: list[str] = []
    for op in ops:
        verb = op.get("op")
        if verb == "set":
            path = str(op.get("path", ""))
            if not path or "." in path or path in {"id", "type", "version"}:
                raise ValueError(f"unsupported {resource_type} set path {path!r}")
            if resource_type == "timeline":
                raise ValueError("timeline is append-only")
            if resource_type == "review" and path not in {
                "reviewed_at",
                "reviewed_by",
                "basis",
                "priority",
            }:
                raise ValueError(f"unsupported review set path {path!r}")
            working[path] = op.get("value")
        elif verb == "append":
            target = op.get("target")
            if resource_type == "timeline" and target in {"timeline", "events"}:
                if len(ops) != 1:
                    raise ValueError("timeline append must be the only op")
                new_version = append_timeline_event(
                    docs_dir, project, dict(op.get("item") or {}), expected_version
                )
                return new_version, warnings
            if resource_type == "sprint" and target == "items":
                item = op.get("item")
                slug = item if isinstance(item, str) else (item or {}).get("slug")
                if not slug:
                    raise ValueError("sprint item requires slug")
                items = list(working.get("items", []))
                if any(
                    (row if isinstance(row, str) else row.get("slug")) == slug
                    for row in items
                ):
                    raise ValueError(f"{slug!r} already exists in sprint {resource_id}")
                items.append(item)
                working["items"] = items
            elif resource_type == "review" and target == "findings":
                finding = dict(op.get("item") or {})
                finding_id = finding.get("id")
                if not finding_id:
                    raise ValueError("findings[].id must be non-empty")
                findings = list(working.get("findings", []))
                if any(row.get("id") == finding_id for row in findings):
                    raise ValueError(f"findings[].id duplicates {finding_id!r}")
                findings.append(finding)
                working["findings"] = findings
            else:
                raise ValueError(
                    f"unsupported append target {target!r} for {resource_type}"
                )
        elif verb == "resolve" and resource_type == "review":
            if op.get("target") != "findings":
                raise ValueError("review resolve target must be findings")
            finding_id = str(op.get("id", ""))
            findings = list(working.get("findings", []))
            match = next(
                (row for row in findings if str(row.get("id", "")) == finding_id),
                None,
            )
            if match is None:
                raise ValueError(f"findings[].id {finding_id!r} was not found")
            match.pop("status", None)
            match["resolved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            match["resolved_by"] = _review_text(op.get("by"), "findings[].resolved_by")
            match["outcome"] = _review_text(op.get("outcome"), "findings[].outcome")
        elif verb == "move" and resource_type == "sprint":
            if op.get("target") != "sprint_item":
                raise ValueError("sprint move target must be sprint_item")
            result = move_sprint_item(
                docs_dir,
                project,
                str(op.get("slug", "")),
                resource_id,
                str(op.get("to", "")),
                expected_version,
                int(op.get("to_version", -1)),
            )
            return result["from_version"], [
                f"destination_version={result['to_version']}"
            ]
        else:
            raise ValueError(f"unsupported {resource_type} op {verb!r}")
    version = write_resource(
        docs_dir,
        project,
        resource_type,
        resource_id,
        working,
        expected_version,
        create=create,
    )
    return version, warnings
