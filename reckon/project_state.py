"""Distributed project-state resources and explicit legacy-index migration.

Project workflow state is stored in independently versioned resources:

* ``docs/sprints/<id>.html``
* ``docs/milestones/<id>.html``
* ``docs/blockers/<id>.html``
* ``docs/state/<project>/timeline.html``
* ``docs/state/<project>/project.json``

The legacy ``index.json`` remains a byte-for-byte migration source and
compatibility snapshot.  A completion marker is the format switch: without it,
the legacy index is canonical; with it, distributed resources are canonical
and missing or malformed resources are errors.  There are no dual writes.
"""

from __future__ import annotations

import hashlib
import html
import base64
import fcntl
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from reckon._schema import Blocker, Milestone, Sprint, TimelineEntry

MARKER_RELATIVE = Path(".reckon/project-state-migration.json")
SNAPSHOT_ROOT = Path(".reckon/snapshots/project-state")
RESOURCE_SCRIPT_ID = "reckon-resource-state"
RESOURCE_TYPES = frozenset({"sprint", "milestone", "blocker", "timeline", "project"})
LIFECYCLE_ITEM_FIELDS = frozenset({"status", "impl"})
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_META_RE = re.compile(
    r"""<meta\b(?=[^>]*\bname=["'](?P<name>[^"']+)["'])(?=[^>]*\bcontent=["'](?P<content>[^"']*)["'])[^>]*>""",
    re.IGNORECASE,
)
_SCRIPT_RE = re.compile(
    rf"""<script\b[^>]*\bid=["']{RESOURCE_SCRIPT_ID}["'][^>]*>(?P<data>.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
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


@dataclass(frozen=True)
class ProjectStateMode:
    """Resolved canonical-format mode for one docs tree."""

    format: str
    marker: dict[str, Any] | None = None


def _safe_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."} or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} must be a single safe path segment")
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
    if resource_type == "project":
        if resource_id != "project":
            raise ValueError("project resource id must be 'project'")
        return docs_dir / "state" / project / "project.json"
    raise ValueError(f"unsupported project resource type {resource_type!r}")


@contextmanager
def _resource_lock(
    docs_dir: Path, project: str, resource_type: str, resource_id: str
):
    """Serialise one resource's version check and atomic replacement."""
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
def _resource_locks(
    docs_dir: Path, project: str, identities: list[tuple[str, str]]
):
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
        state.get("theme")
        or state.get("name")
        or state.get("summary")
        or resource_id
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
    else:
        rows.append(
            f'<dl data-reckon="{resource_type}"><dt>Identity</dt>'
            f"<dd>{html.escape(resource_id)}</dd></dl>"
        )
    state_json = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).replace("</", "<\\/")
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "  <meta charset=\"utf-8\">\n"
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
        raise ProjectStateError(f"distributed project resource is malformed: {path}") from exc
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
    recover_project_state_transactions(docs_dir, project)
    return _read_resource_unchecked(
        docs_dir, project, resource_type, resource_id
    )


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
    return data, int(data.get("version", 0) or 0)


def _validate_resource(resource_type: str, data: dict[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(data)
    if resource_type == "sprint":
        sprint = Sprint.model_validate(cleaned)
        cleaned = sprint.model_dump(
            exclude_none=True, exclude_unset=True, by_alias=True
        )
        cleaned["id"] = data.get("id", sprint.id)
        cleaned["version"] = int(data.get("version", 0) or 0)
        for item in cleaned.get("items", []):
            for field in LIFECYCLE_ITEM_FIELDS:
                item.pop(field, None)
    elif resource_type == "milestone":
        cleaned = Milestone.model_validate(cleaned).model_dump(
            exclude_none=True, exclude_unset=True
        )
        cleaned["version"] = int(data.get("version", 0) or 0)
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
            ids.add(ident)
            validated.append(item)
        cleaned = {
            "id": "timeline",
            "type": "timeline",
            "version": int(data.get("version", 0) or 0),
            "events": validated,
        }
    elif resource_type == "project":
        forbidden = {
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
        }
        bad = forbidden & set(cleaned)
        if bad:
            raise ValueError(
                "project manifest is identity/presentation-only; forbidden keys: "
                + ", ".join(sorted(bad))
            )
        cleaned["version"] = int(data.get("version", 0) or 0)
    cleaned["type"] = resource_type
    return cleaned


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
            raise FileNotFoundError(
                f"{resource_type} {resource_id!r} does not exist"
            )
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
        tmp.replace(path)
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


def _identity_manifest(project: str, legacy: dict[str, Any]) -> dict[str, Any]:
    project_rows = legacy.get("projects") or []
    row = dict(project_rows[0]) if project_rows and isinstance(project_rows[0], dict) else {}
    return {
        "project": project,
        "owner": row.get("owner", ""),
        "published": row.get("published", ""),
        "version": 0,
    }


def _migration_payloads(
    project: str, legacy: dict[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    for sprint in legacy.get("sprints", []):
        record = dict(sprint)
        sid = str(record.get("id", ""))
        _safe_segment(sid, "sprint id")
        for item in record.get("items", []):
            if isinstance(item, dict):
                for field in LIFECYCLE_ITEM_FIELDS:
                    item.pop(field, None)
        record.update({"id": sid, "type": "sprint", "version": 0})
        payloads[("sprint", sid)] = record
    for milestone in legacy.get("milestones", []):
        record = dict(milestone)
        mid = str(record.get("id", ""))
        _safe_segment(mid, "milestone id")
        record.update({"id": mid, "type": "milestone", "version": 0})
        payloads[("milestone", mid)] = record
    for position, blocker in enumerate(legacy.get("blockers", [])):
        record = dict(blocker)
        bid = str(record.get("id") or f"blocker-{position + 1}")
        _safe_segment(bid, "blocker id")
        record.pop("n", None)
        record.update({"id": bid, "type": "blocker", "version": 0})
        payloads[("blocker", bid)] = record
    events = []
    for position, event in enumerate(legacy.get("timeline", [])):
        record = dict(event)
        record["id"] = str(record.get("id") or _event_id(record, position))
        events.append(record)
    payloads[("timeline", "timeline")] = {
        "id": "timeline",
        "type": "timeline",
        "version": 0,
        "events": events,
    }
    payloads[("project", "project")] = _identity_manifest(project, legacy)
    return payloads


def _plan_state_by_slug(docs_dir: Path, project: str) -> dict[str, dict[str, Any]]:
    from reckon import _plan_html
    from reckon.resources import resource_map

    result: dict[str, dict[str, Any]] = {}
    for resource in resource_map(
        docs_dir, project, include_archived=False
    ).values():
        if resource.type != "plan":
            continue
        result[resource.slug] = _plan_html.read_state(
            resource.path.read_text(encoding="utf-8", errors="replace")
        )
    return result


def _derive_active_sprint(sprints: list[dict[str, Any]]) -> str | None:
    active = [str(item.get("id")) for item in sprints if item.get("status") == "active"]
    if len(active) > 1:
        raise ProjectStateError(
            "multiple active sprints violate derive-unique-active: "
            + ", ".join(active)
        )
    return active[0] if active else None


def _derive_blocker_counts(
    blockers: list[dict[str, Any]], sprints: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for sprint in sprints:
        for item in sprint.get("items", []):
            if not isinstance(item, dict):
                continue
            for blocker_id in item.get("blocked_by", []):
                counts[str(blocker_id)] = counts.get(str(blocker_id), 0) + 1
    return [{**item, "n": counts.get(str(item.get("id")), 0)} for item in blockers]


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
                for key in ("title", "status", "impl"):
                    if key in state:
                        item[key] = state[key]
            items.append(item)
        sprint["items"] = items
    return hydrated


def compose_project_state(docs_dir: Path, project: str) -> dict[str, Any]:
    """Compose the distributed resources into the compatibility index shape."""
    mode = project_state_mode(docs_dir)
    if mode.format != "distributed":
        data, _ = _load_legacy_index(docs_dir, project)
        return {**deepcopy(data), "source_format": "legacy-index"}

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
            raise ProjectStateError(f"duplicate distributed resource identity: {key_tuple}")
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
    sprints.sort(key=lambda item: str(item.get("id", "")))
    milestones.sort(key=lambda item: str(item.get("id", "")))
    blockers.sort(key=lambda item: str(item.get("id", "")))
    hydrated_sprints = _hydrate_items(docs_dir, project, sprints)
    active_id = _derive_active_sprint(hydrated_sprints)
    return {
        "_version": 0,
        "active_sprint_id": active_id,
        "projects": [project_manifest],
        "sprints": hydrated_sprints,
        "milestones": milestones,
        "blockers": _derive_blocker_counts(blockers, hydrated_sprints),
        "timeline": timeline,
        "source_format": "distributed",
        "resource_versions": versions,
    }


def audit_project_state(docs_dir: Path, project: str) -> list[dict[str, str]]:
    """Report distributed resource and unique-active invariant violations."""
    if project_state_mode(docs_dir).format != "distributed":
        return []
    try:
        compose_project_state(docs_dir, project)
    except ProjectStateError as exc:
        message = str(exc)
        code = (
            "multiple-active-sprints"
            if message.startswith("multiple active sprints")
            else "distributed-project-state-invalid"
        )
        return [{"code": code, "severity": "error", "message": message}]
    return []


def _parity_projection(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise old/new views to durable, non-derived project state."""
    result = {
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
        for field in ("description", "starts", "ends", "summary"):
            if not record.get(field):
                record.pop(field, None)
        for item in record.get("items", []):
            if isinstance(item, dict):
                for field in (*LIFECYCLE_ITEM_FIELDS, "title"):
                    item.pop(field, None)
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
        record.pop("id", None)
        result["timeline"].append(record)
    for key in result:
        result[key] = sorted(
            result[key], key=lambda item: _canonical_json(item)
        )
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


def migrate_project_state(
    docs_dir: Path,
    project: str,
    *,
    before_install: Callable[[], None] | None = None,
    install_hook: Callable[[int, Path, Path], None] | None = None,
) -> dict[str, Any]:
    """Explicitly split the legacy index and publish the format marker last.

    ``before_install`` and ``install_hook`` are test seams for source mutation
    and injected installation failure.  A failed install leaves the marker
    absent, therefore legacy mode remains canonical.
    """
    docs_dir = docs_dir.resolve()
    _safe_segment(project, "project")
    legacy, source_bytes = _load_legacy_index(docs_dir, project)
    if not source_bytes:
        raise ProjectStateError("legacy index does not exist")
    source_sha = _sha256_bytes(source_bytes)
    mode = project_state_mode(docs_dir)
    if mode.format == "distributed":
        marker = mode.marker or {}
        if marker.get("source_sha256") != source_sha:
            raise ProjectStateError(
                "legacy index changed after distributed activation; "
                "restore the recorded source or perform a new reviewed migration"
            )
        # Validate that every declared resource still exists and has the
        # recorded identity.  Resource hashes/versions are migration evidence,
        # not immutable runtime checks: normal distributed edits change them.
        compose_project_state(docs_dir, project)
        return {"ok": True, "changed": False, **marker}

    snapshot = docs_dir / SNAPSHOT_ROOT / source_sha / "index.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists() and snapshot.read_bytes() != source_bytes:
        raise ProjectStateError(f"snapshot collision: {snapshot}")
    snapshot.write_bytes(source_bytes)

    payloads = _migration_payloads(project, deepcopy(legacy))
    stage_root = Path(
        tempfile.mkdtemp(prefix="project-state-", dir=docs_dir / ".reckon")
    )
    staging_docs = stage_root / "docs"
    staged: list[tuple[str, str, Path]] = []
    try:
        for (resource_type, resource_id), payload in sorted(payloads.items()):
            staged_path = _write_staged_resource(
                staging_docs, project, resource_type, resource_id, payload
            )
            read_resource(staging_docs, project, resource_type, resource_id)
            staged.append((resource_type, resource_id, staged_path))

        # Compose a temporary distributed view and prove durable parity.
        temp_rows = [
            {
                "type": resource_type,
                "id": resource_id,
                "sha256": _sha256_path(path),
                "version": int(payloads[(resource_type, resource_id)].get("version", 0)),
            }
            for resource_type, resource_id, path in staged
        ]
        temp_marker = {
            "format": "distributed",
            "status": "complete",
            "source_sha256": source_sha,
            "resources": temp_rows,
        }
        temp_marker_path = marker_path(staging_docs)
        temp_marker_path.parent.mkdir(parents=True, exist_ok=True)
        temp_marker_path.write_text(json.dumps(temp_marker), encoding="utf-8")
        composed = compose_project_state(staging_docs, project)
        old_parity = _sha256_bytes(_canonical_json(_parity_projection(legacy)))
        new_parity = _sha256_bytes(_canonical_json(_parity_projection(composed)))
        if old_parity != new_parity:
            raise ProjectStateError("composed distributed state failed parity check")

        if before_install:
            before_install()
        if legacy_index_path(docs_dir, project).read_bytes() != source_bytes:
            raise ProjectStateError("legacy index changed while migration was staged")

        installed: list[tuple[str, str, Path]] = []
        for position, (resource_type, resource_id, source) in enumerate(staged):
            destination = resource_path(docs_dir, project, resource_type, resource_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if install_hook:
                install_hook(position, source, destination)
            os.replace(source, destination)
            installed.append((resource_type, resource_id, destination))

        rows = [
            {
                "type": resource_type,
                "id": resource_id,
                "path": str(path.relative_to(docs_dir)),
                "sha256": _sha256_path(path),
                "version": read_resource(
                    docs_dir, project, resource_type, resource_id
                )[1],
            }
            for resource_type, resource_id, path in installed
        ]
        marker = {
            "format": "distributed",
            "status": "complete",
            "project": project,
            "source": str(legacy_index_path(docs_dir, project).relative_to(docs_dir)),
            "source_sha256": source_sha,
            "source_version": int(legacy.get("_version", 0) or 0),
            "snapshot": str(snapshot.relative_to(docs_dir)),
            "snapshot_sha256": _sha256_path(snapshot),
            "parity_sha256": old_parity,
            "resources": rows,
            "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        target = marker_path(docs_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        marker_tmp = target.with_suffix(".json.tmp")
        marker_tmp.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        marker_tmp.replace(target)
        return {"ok": True, "changed": True, **marker}
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
    source_path: Path,
    target_path: Path,
    source_bytes: bytes,
    target_bytes: bytes,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "sprint-item-move",
        "status": "prepared",
        "project": project,
        "source": str(source_path),
        "target": str(target_path),
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
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def recover_project_state_transactions(docs_dir: Path, project: str) -> list[Path]:
    """Restore both sides of any prepared sprint-move journal."""
    recovered: list[Path] = []
    root = docs_dir / ".reckon" / "transactions"
    if not root.is_dir():
        return recovered
    for journal in sorted(root.glob("sprint-move-*.json")):
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            if (
                payload.get("kind") != "sprint-item-move"
                or payload.get("status") != "prepared"
                or payload.get("project") != project
            ):
                raise ProjectStateError(f"malformed transaction journal: {journal}")
            source_path = Path(payload["source"])
            target_path = Path(payload["target"])
            source_bytes = base64.b64decode(payload["source_before"])
            target_bytes = base64.b64decode(payload["target_before"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ProjectStateError(f"malformed transaction journal: {journal}") from exc
        with _resource_locks(
            docs_dir,
            project,
            [
                ("sprint", source_path.stem),
                ("sprint", target_path.stem),
            ],
        ):
            for path, content in (
                (source_path, source_bytes),
                (target_path, target_bytes),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".recover", dir=path.parent
                )
                os.close(fd)
                tmp = Path(tmp_name)
                try:
                    tmp.write_bytes(content)
                    tmp.replace(path)
                finally:
                    tmp.unlink(missing_ok=True)
            journal.unlink()
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
    journal = _move_journal_path(
        docs_dir, project, from_sprint, to_sprint, slug
    )
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
            source_path,
            target_path,
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
                    tmp.replace(path)
                finally:
                    tmp.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            raise
        journal.unlink(missing_ok=True)
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
        working, current = read_resource(
            docs_dir, project, resource_type, resource_id
        )
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
            else:
                raise ValueError(
                    f"unsupported append target {target!r} for {resource_type}"
                )
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
