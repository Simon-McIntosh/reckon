"""Progressive, human-readable response views for Reckon's MCP tools."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reckon.doccheck import lifecycle_staleness, modified_age_days
from reckon.lifecycle import (
    TERMINAL_STATUSES,
    effective_status,
    unpassed_gate_blockers,
    unresolved_dependencies,
)
from reckon.project_state import _natural_identifier_key

VIEW_NAMES = frozenset({"summary", "detail", "history", "version", "raw", "schema"})
RESOURCE_TYPES = frozenset(
    {
        "plan",
        "research",
        "evidence",
        "sprint",
        "milestone",
        "blocker",
        "timeline",
        "project",
        "review",
        "audit",
    }
)
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
RESPONSE_SCHEMA_VERSION = 2
MAX_SELECTOR_LENGTH = 128
MAX_CURSOR_LENGTH = 256
MAX_ERROR_TEXT_LENGTH = 512
MAX_ERROR_COLLECTION_ITEMS = 25
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def sprint_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise live sprint-member lifecycle state without storing it."""

    rows = [item for item in items if isinstance(item, dict)]
    counts: dict[str, int] = {}
    current_work: list[dict[str, Any]] = []
    implementations: list[float] = []
    for item in rows:
        status = str(item.get("effective_status") or item.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
        impl = float(item.get("impl", 0.0) or 0.0)
        implementations.append(impl)
        if status not in TERMINAL_STATUSES and 0.0 < impl < 1.0:
            current_work.append(
                {
                    key: item[key]
                    for key in ("slug", "title", "effective_status", "impl")
                    if item.get(key) is not None
                }
            )
    return {
        "item_count": len(rows),
        "by_effective_status": counts,
        "mean_impl": round(sum(implementations) / len(rows), 4) if rows else 0.0,
        "current_work": current_work,
    }


def compose_review(
    review: dict[str, Any],
    inventory: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    project: str,
    project_resources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Join a stored review to current project state."""

    from copy import deepcopy
    from datetime import date

    from reckon._schema import parse_plan_ref

    plans = {
        str(item.get("slug")): item
        for item in inventory
        if isinstance(item, dict)
        and item.get("type", "plan") == "plan"
        and item.get("slug")
    }
    resources: dict[tuple[str, str], dict[str, Any]] = {
        ("plan", slug): item for slug, item in plans.items()
    }
    resource_groups = project_resources or {}
    for kind, rows in (
        ("sprint", sprints),
        ("milestone", resource_groups.get("milestones") or []),
        ("blocker", resource_groups.get("blockers") or []),
    ):
        resources.update(
            {
                (kind, str(item.get("id"))): item
                for item in rows
                if isinstance(item, dict) and item.get("id")
            }
        )
    resources[("project", project)] = {"id": project, "status": "active"}

    def subject_row(subject: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(subject.get("kind") or "")
        subject_id = str(subject.get("id") or "")
        if kind == "plan":
            ref = parse_plan_ref(subject_id)
            if ref is None or ref.is_external(project):
                return None
            subject_id = ref.slug
        return resources.get((kind, subject_id))

    def action_satisfied(verb: str, status: str) -> bool:
        if verb == "close":
            return status in TERMINAL_STATUSES
        if verb == "reopen":
            return bool(status) and status not in TERMINAL_STATUSES
        if verb == "resolve":
            return status in {*TERMINAL_STATUSES, "resolved", "closed"}
        return False

    composed = deepcopy(review)
    reviewed_at = str(composed.get("reviewed_at") or "")
    findings = []
    for finding in composed.get("findings") or []:
        row = dict(finding)
        subject = row.get("subject") if isinstance(row.get("subject"), dict) else {}
        live = subject_row(subject)
        status = str((live or {}).get("status") or "")
        checked_at = str(row.get("checked_at") or "")
        changed_at = str((live or {}).get("modified") or (live or {}).get("last") or "")
        moved = False
        try:
            moved = bool(changed_at) and date.fromisoformat(
                changed_at[:10]
            ) > date.fromisoformat(checked_at)
        except ValueError:
            moved = False
        row.update(
            {
                "subject_found": live is not None,
                "subject_status": status,
                "stale": moved and not bool(row.get("resolved_at")),
                "current": bool(live)
                and not row.get("resolved_at")
                and not moved
                and not action_satisfied(
                    str((row.get("recommended_action") or {}).get("verb") or ""),
                    status,
                ),
            }
        )
        findings.append(row)
    composed["findings"] = findings

    priority = []
    ranked_sprints: list[str] = []
    stored_priority = sorted(
        (row for row in composed.get("priority") or [] if isinstance(row, dict)),
        key=lambda row: (int(row.get("rank", 10**6)), str(row.get("ref") or "")),
    )
    for stored in stored_priority:
        row = dict(stored)
        ref = parse_plan_ref(str(row.get("ref") or ""))
        live = None if ref is None or ref.is_external(project) else plans.get(ref.slug)
        status = str((live or {}).get("status") or "")
        effective = str((live or {}).get("effective_status") or status)
        sprint = (live or {}).get("sprint")
        landed = effective in TERMINAL_STATUSES
        modified = str((live or {}).get("modified") or (live or {}).get("last") or "")
        moved = False
        try:
            moved = bool(modified and reviewed_at) and date.fromisoformat(
                modified[:10]
            ) > date.fromisoformat(reviewed_at[:10])
        except ValueError:
            moved = False
        row.update(
            {
                "status": status,
                "effective_status": effective,
                "impl": float((live or {}).get("impl", 0.0) or 0.0),
                "sprint": sprint,
                "landed": landed,
                "stale": moved and not landed,
            }
        )
        if sprint and sprint not in ranked_sprints:
            ranked_sprints.append(str(sprint))
        priority.append(row)
    composed["priority"] = priority
    open_sprints = sorted(
        (
            str(sprint.get("id"))
            for sprint in sprints
            if isinstance(sprint, dict)
            and sprint.get("id")
            and str(sprint.get("status") or "planned") not in TERMINAL_STATUSES
        ),
        key=_natural_identifier_key,
    )
    composed["sprint_order"] = ranked_sprints + [
        sprint_id for sprint_id in open_sprints if sprint_id not in ranked_sprints
    ]
    return composed


def load_composed_review(
    docs_dir: Path,
    project: str,
    inventory: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    project_resources: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, int | None]:
    """Read and compose the optional review singleton through one shared path."""

    from reckon.project_state import ProjectStateError, read_resource, resource_path

    if not resource_path(docs_dir, project, "review", "review").is_file():
        return None, None
    try:
        review, version = read_resource(docs_dir, project, "review", "review")
    except (OSError, ProjectStateError, ValueError):
        return None, None
    return (
        compose_review(review, inventory, sprints, project, project_resources),
        version,
    )


def in_flight_by_plan(
    project: str,
    pointers: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Group the live runs for one project by their target plan."""

    if pointers is None:
        from reckon import crew

        try:
            pointers = crew.list_live()
        except OSError:
            pointers = []

    grouped: dict[str, list[dict[str, str]]] = {}
    for pointer in pointers:
        if not isinstance(pointer, dict) or pointer.get("project") != project:
            continue
        node = pointer.get("node")
        if not isinstance(node, dict):
            continue
        plan = str(node.get("plan") or "").strip()
        if not plan:
            continue
        grouped.setdefault(plan, []).append(
            {
                "run_id": str(pointer.get("run_id") or ""),
                "member": str(pointer.get("member") or ""),
                "section": str(node.get("section") or ""),
                "started_at": str(pointer.get("created_at") or ""),
            }
        )
    for runs in grouped.values():
        runs.sort(key=lambda run: run["run_id"])
    return grouped


class ViewRequestError(ValueError):
    """A stable, agent-readable request error."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class ResourceSelector:
    """Stable typed identity used by every progressive response."""

    project: str
    type: str
    id: str
    archived: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "type": self.type,
            "id": self.id,
            "archived": self.archived,
        }


def compact_size(value: Any) -> int:
    """Return deterministic compact UTF-8 JSON size."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def storage_schema_for(resource_type: str) -> dict[str, Any]:
    """Return the storage contract for one canonical resource type."""

    from reckon._schema import Blocker, Milestone, Sprint, TimelineEntry

    if resource_type in {"plan", "research", "evidence"}:
        from reckon._schema import gen_json_schema

        return gen_json_schema()

    if resource_type in {"sprint", "milestone", "blocker"}:
        model = {
            "sprint": Sprint,
            "milestone": Milestone,
            "blocker": Blocker,
        }[resource_type]
        schema = model.model_json_schema()
        schema["title"] = f"reckon {resource_type.title()}Resource"
        schema["schemaVersion"] = RESPONSE_SCHEMA_VERSION
        properties = schema.setdefault("properties", {})
        properties["type"] = {"const": resource_type, "type": "string"}
        properties["version"] = {"minimum": 0, "type": "integer"}
        schema["required"] = sorted(
            set(schema.get("required") or []) | {"id", "type", "version"}
        )
        return schema

    if resource_type == "timeline":
        return {
            "title": "reckon TimelineResource",
            "schemaVersion": RESPONSE_SCHEMA_VERSION,
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "type", "version", "events"],
            "properties": {
                "id": {"const": "timeline", "type": "string"},
                "type": {"const": "timeline", "type": "string"},
                "version": {"minimum": 0, "type": "integer"},
                "events": {
                    "type": "array",
                    "items": TimelineEntry.model_json_schema(),
                },
            },
        }

    if resource_type == "project":
        return {
            "title": "reckon ProjectResource",
            "schemaVersion": RESPONSE_SCHEMA_VERSION,
            "type": "object",
            "additionalProperties": True,
            "required": ["project", "type", "version"],
            "properties": {
                "project": {"type": "string"},
                "type": {"const": "project", "type": "string"},
                "version": {"minimum": 0, "type": "integer"},
                "owner": {"type": "string"},
                "published": {"type": "string"},
                "scope": {
                    "type": "object",
                    "properties": {
                        "owns": {"type": "array", "items": {"type": "string"}},
                        "excludes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "routes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["work", "project"],
                                "properties": {
                                    "work": {"type": "string"},
                                    "project": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        }
    if resource_type == "review":
        return {
            "title": "reckon ReviewResource",
            "schemaVersion": RESPONSE_SCHEMA_VERSION,
            "type": "object",
            "additionalProperties": True,
            "required": [
                "id",
                "type",
                "version",
                "reviewed_at",
                "reviewed_by",
                "basis",
                "findings",
                "priority",
            ],
        }

    raise ViewRequestError(
        "invalid_resource",
        f"No storage schema exists for resource type {resource_type!r}.",
    )


def normalize_view(view: str | None) -> str:
    """Validate a view name, defaulting typed calls to ``summary``."""

    selected = (view or "summary").strip().lower()
    if selected not in VIEW_NAMES:
        choices = ", ".join(sorted(VIEW_NAMES))
        displayed = repr(view)
        if len(displayed) > 96:
            displayed = displayed[:93] + "..."
        raise ViewRequestError(
            "invalid_view",
            f"Unknown MCP response view {displayed}.",
            f"Choose one of: {choices}.",
        )
    return selected


def normalize_selector(
    resource: dict[str, Any],
    *,
    fallback_project: str | None = None,
) -> ResourceSelector:
    """Validate the public ``resource`` selector."""

    if not isinstance(resource, dict):
        raise ViewRequestError(
            "invalid_resource",
            "resource must be an object with project, type, and id fields.",
        )
    project = resource.get("project") or fallback_project
    resource_type = str(resource.get("type") or "").strip().lower()
    if resource_type == "doc":
        resource_type = "research"
    resource_id = resource.get("id")
    archived = resource.get("archived", False)
    if not isinstance(project, str) or not project.strip():
        raise ViewRequestError("invalid_resource", "resource.project is required.")
    if resource_type not in RESOURCE_TYPES - {"audit"}:
        raise ViewRequestError(
            "invalid_resource",
            f"Unsupported resource type {resource_type!r}.",
            "Use plan, research, evidence, sprint, milestone, blocker, timeline, or project.",
        )
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ViewRequestError("invalid_resource", "resource.id is required.")
    for label, value in (("project", project.strip()), ("id", resource_id.strip())):
        if (
            len(value) > MAX_SELECTOR_LENGTH
            or value in {".", ".."}
            or not _SAFE_SEGMENT.fullmatch(value)
        ):
            raise ViewRequestError(
                "invalid_resource",
                f"resource.{label} must be one safe path segment.",
            )
    if not isinstance(archived, bool):
        raise ViewRequestError(
            "invalid_resource", "resource.archived must be true or false."
        )
    return ResourceSelector(
        project=project.strip(),
        type=resource_type,
        id=resource_id.strip(),
        archived=archived,
    )


def error_response(
    error: str,
    message: str,
    *,
    selector: ResourceSelector | None = None,
    operation: str = "read",
    hint: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Build the bounded structured error contract."""

    def bounded(value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            if len(value) <= MAX_ERROR_TEXT_LENGTH:
                return value
            return value[: MAX_ERROR_TEXT_LENGTH - 3] + "..."
        if depth >= 4:
            return "<omitted>"
        if isinstance(value, dict):
            return {
                str(key)[:MAX_ERROR_TEXT_LENGTH]: bounded(item, depth + 1)
                for key, item in list(value.items())[:MAX_ERROR_COLLECTION_ITEMS]
            }
        if isinstance(value, (list, tuple)):
            return [
                bounded(item, depth + 1) for item in value[:MAX_ERROR_COLLECTION_ITEMS]
            ]
        return value

    result: dict[str, Any] = {
        "ok": False,
        "error": error,
        "message": message,
        "operation": operation,
    }
    if selector is not None:
        result["resource"] = selector.as_dict()
    result.update(extra)
    if hint:
        result["hint"] = hint
    return bounded(result)


def _cursor(offset: int) -> str:
    raw = json.dumps({"offset": offset}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    if not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_LENGTH:
        raise ViewRequestError(
            "invalid_cursor",
            "The pagination cursor is invalid.",
            "Restart from the first page by omitting cursor.",
        )
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        offset = value["offset"]
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or set(value) != {"offset"}
        ):
            raise ValueError
        return offset
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ViewRequestError(
            "invalid_cursor",
            "The pagination cursor is invalid.",
            "Restart from the first page by omitting cursor.",
        ) from exc


def paginate(
    records: list[Any],
    *,
    cursor: str | None,
    limit: int | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Return one deterministic cursor page."""

    if limit is None:
        page_size = DEFAULT_PAGE_SIZE
    elif not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ViewRequestError("invalid_limit", "limit must be a positive integer.")
    else:
        page_size = min(limit, MAX_PAGE_SIZE)
    offset = _cursor_offset(cursor)
    if offset > len(records):
        raise ViewRequestError(
            "invalid_cursor",
            "The pagination cursor points beyond the available records.",
            "Restart from the first page by omitting cursor.",
        )
    page = records[offset : offset + page_size]
    next_offset = offset + len(page)
    return page, {
        "count": len(page),
        "total": len(records),
        "next_cursor": _cursor(next_offset) if next_offset < len(records) else None,
    }


def _open_decisions(data: dict[str, Any]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for key, value in (data.get("decisions") or {}).items():
        if not isinstance(value, dict) or value.get("choice"):
            continue
        decisions.append(
            {
                "key": key,
                "question": value.get("title", ""),
                "options": list(value.get("choices") or []),
            }
        )
    return decisions


def _followup_record(item: dict[str, Any], *, include_prompts: bool) -> dict[str, Any]:
    result = {
        "id": item.get("id", ""),
        "title": item.get("title", ""),
        "body": item.get("body", ""),
        "recommends_skill": item.get("recommends_skill", ""),
        "capability": item.get("capability") or {},
    }
    if include_prompts:
        result["prompt"] = item.get("prompt", "")
    return result


def _next_action(
    data: dict[str, Any], *, include_prompts: bool
) -> dict[str, Any] | None:
    for item in data.get("followups") or []:
        if isinstance(item, dict) and item.get("status", "open") == "open":
            return _followup_record(item, include_prompts=include_prompts)
    return None


def _relations(data: dict[str, Any]) -> dict[str, list[Any]]:
    relations: dict[str, list[Any]] = {}
    for field in (
        "depends_on",
        "blocks",
        "informs",
        "evidence_for",
        "verifies",
        "supersedes",
    ):
        value = data.get(field)
        if value:
            relations[field] = list(value) if isinstance(value, list) else [value]
    return relations


def _blocking(data: dict[str, Any], deps: list[dict[str, Any]]) -> list[Any]:
    explicit = data.get("blocked_by")
    result = list(explicit) if isinstance(explicit, list) else []
    result.extend(unresolved_dependencies(deps))
    result.extend(unpassed_gate_blockers(data.get("gates") or []))
    return result


def _plan_effort(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the plan estimate and its derived consumption in named units."""

    raw_hours = data.get("effort_hours")
    if raw_hours is None:
        return None
    estimated_hours = float(raw_hours)
    progress = float(data.get("impl", 0.0) or 0.0)
    spent_hours = estimated_hours * progress
    return {
        "estimated_hours": round(estimated_hours, 2),
        "spent_hours": round(spent_hours, 2),
        "remaining_hours": round(estimated_hours - spent_hours, 2),
        "unit": "worker-hours",
    }


def _sprint_capacity(data: dict[str, Any]) -> dict[str, Any]:
    """Return the summed plan estimates for a sprint in named units."""

    capacity = data.get("capacity")
    if isinstance(capacity, dict) and capacity.get("unit") == "worker-hours":
        return capacity
    total_hours = sum(
        float(item.get("effort_hours") or 0.0)
        for item in data.get("items") or []
        if isinstance(item, dict)
    )
    return {"total_hours": round(total_hours, 2), "unit": "worker-hours"}


def _state(
    selector: ResourceSelector,
    data: dict[str, Any],
    blocking: list[Any],
) -> dict[str, Any]:
    resource_type = selector.type
    if resource_type == "plan":
        workflow_status = str(data.get("status") or "draft")
        age_days = modified_age_days(data.get("modified"))
        state = {
            "status": workflow_status,
            "effective_status": effective_status(workflow_status, blocking),
            "progress": float(data.get("impl", 0.0) or 0.0),
            "age_days": age_days,
            "staleness": lifecycle_staleness(
                doc_type="plan",
                status=workflow_status,
                impl=data.get("impl"),
                age_days=age_days,
            ),
            "sprint": data.get("sprint") or None,
            "milestone": data.get("milestone") or None,
            "capability": data.get("capability") or {},
        }
        effort = _plan_effort(data)
        if effort is not None:
            state["effort"] = effort
        return state
    if resource_type == "research":
        reviewed_at = data.get("reviewed_at", "")
        return {
            "reviewed": bool(reviewed_at),
            "reviewed_at": reviewed_at,
            "source_quality": data.get("source_quality", ""),
        }
    if resource_type == "evidence":
        return {
            "verdict": data.get("verdict", ""),
            "recorded_at": data.get("recorded_at", ""),
            "environment": data.get("environment", ""),
        }
    if resource_type == "sprint":
        items = [item for item in data.get("items") or [] if isinstance(item, dict)]
        statuses = [
            str(item.get("effective_status") or item.get("status") or "pending")
            for item in items
        ]
        return {
            "status": data.get("status", "planned"),
            "starts": data.get("starts", ""),
            "ends": data.get("ends", ""),
            "items": len(items),
            "completed": sum(status in {"shipped", "done"} for status in statuses),
            "blocked": sum(status == "blocked" for status in statuses),
            "metrics": data.get("metrics") or sprint_metrics(items),
            "capacity": _sprint_capacity(data),
        }
    if resource_type == "milestone":
        return {
            "status": data.get("status", "planned"),
            "progress": float(data.get("pct", data.get("progress", 0)) or 0),
        }
    if resource_type == "blocker":
        return {
            "status": data.get("status", "open"),
            "owner": data.get("owner", ""),
            "next": data.get("next", ""),
        }
    if resource_type == "timeline":
        return {"events": len(data.get("events") or [])}
    if resource_type == "project":
        summary = data.get("summary") or {}
        return {
            "active_sprint_id": data.get("active_sprint_id"),
            "plans": summary.get("plans", 0),
            "artifacts": summary.get("artifacts", 0),
            "open_followups": summary.get("open_followups", 0),
            "open_questions": summary.get("open_questions", 0),
            "open_decisions": summary.get("open_decisions", 0),
        }
    if resource_type == "review":
        findings = list(data.get("findings") or [])
        priority = list(data.get("priority") or [])
        return {
            "reviewed_at": data.get("reviewed_at", ""),
            "reviewed_by": data.get("reviewed_by", ""),
            "findings": len(findings),
            "current_findings": sum(bool(row.get("current")) for row in findings),
            "priority_rows": len(priority),
            "sprint_order": list(data.get("sprint_order") or []),
        }
    return {}


def _summary(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    deps: list[dict[str, Any]],
    include_prompts: bool,
) -> dict[str, Any]:
    if include_prompts:
        raise ViewRequestError(
            "invalid_view_option",
            "Summary responses never include full followup prompts.",
            "Use view='detail' with include_prompts=true.",
        )
    blocking = _blocking(data, deps)
    return {
        "resource": selector.as_dict(),
        "version": version,
        "view": "summary",
        "title": data.get("title")
        or data.get("theme")
        or data.get("name")
        or selector.id,
        "summary": data.get("summary") or data.get("description") or "",
        "state": _state(selector, data, blocking),
        "blocking": blocking,
        "open_decisions": _open_decisions(data),
        "next": _next_action(data, include_prompts=False),
        "warnings": list(data.get("compatibility_warnings") or []),
    }


def _detail(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    deps: list[dict[str, Any]],
    include_prompts: bool,
) -> dict[str, Any]:
    result = _summary(selector, version, data, deps=deps, include_prompts=False)
    result["view"] = "detail"
    result["metadata"] = {
        key: data[key]
        for key in (
            "owner",
            "modified",
            "roi",
            "source",
            "source_quality",
            "verdict",
            "environment",
        )
        if data.get(key) not in (None, "")
    }
    result["relations"] = _relations(data)
    result["followups"] = [
        _followup_record(item, include_prompts=include_prompts)
        for item in data.get("followups") or []
        if isinstance(item, dict) and item.get("status", "open") == "open"
    ]
    result["questions"] = [
        item
        for item in data.get("questions") or []
        if isinstance(item, dict) and item.get("status", "open") == "open"
    ]
    if selector.type == "sprint":
        result["items"] = [
            {
                key: item.get(key)
                for key in (
                    "slug",
                    "title",
                    "status",
                    "effective_status",
                    "impl",
                    "capability",
                )
                if item.get(key) is not None
            }
            | ({"effort": _plan_effort(item)} if _plan_effort(item) is not None else {})
            for item in data.get("items") or []
            if isinstance(item, dict)
        ]
    elif selector.type == "timeline":
        result["events"] = list(data.get("events") or [])
    elif selector.type == "review":
        result["findings"] = list(data.get("findings") or [])
        result["priority"] = list(data.get("priority") or [])
        result["sprint_order"] = list(data.get("sprint_order") or [])
    return result


def _history_records(
    selector: ResourceSelector, data: dict[str, Any], *, include_prompts: bool
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key, item in (data.get("decisions") or {}).items():
        if isinstance(item, dict) and item.get("choice"):
            records.append({"kind": "decision", "key": key, **item})
    for item in data.get("followups") or []:
        if isinstance(item, dict) and item.get("status") == "resolved":
            record = {"kind": "followup", **item}
            if not include_prompts:
                record.pop("prompt", None)
            records.append(record)
    for item in data.get("questions") or []:
        if isinstance(item, dict) and item.get("status") == "resolved":
            records.append({"kind": "question", **item})
    if selector.type == "timeline":
        records.extend(
            {"kind": "timeline", **item}
            for item in data.get("events") or []
            if isinstance(item, dict)
        )
    return records


def _response_schema(
    selector: ResourceSelector,
    view: str,
    *,
    context: str = "resource",
) -> dict[str, Any]:
    pagination = {
        "type": "object",
        "required": ["count", "total", "next_cursor"],
        "properties": {
            "count": {"type": "integer"},
            "total": {"type": "integer"},
            "next_cursor": {"type": ["string", "null"]},
        },
    }
    common: dict[str, Any] = {
        "type": "object",
        "required": ["resource", "version", "view"],
        "properties": {
            "resource": {
                "type": "object",
                "required": ["project", "type", "id", "archived"],
            },
            "version": {"type": "integer"},
            "view": {"const": view},
        },
    }
    if context != "audit":
        common["required"].append("provenance")
        common["properties"]["provenance"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["checkout", "branch", "content_digest"],
            "properties": {
                "checkout": {"type": "string"},
                "branch": {"type": "string"},
                "content_digest": {
                    "type": "string",
                    "pattern": "^sha256:[0-9a-f]{64}$",
                },
            },
        }
    if selector.type == "plan" and view not in {"schema", "version"}:
        common["properties"]["in_flight"] = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["run_id", "member", "section", "started_at"],
                "properties": {
                    "run_id": {"type": "string"},
                    "member": {"type": "string"},
                    "section": {"type": "string"},
                    "started_at": {"type": "string"},
                },
            },
        }
    if view in {"summary", "detail"}:
        common["required"] += [
            "title",
            "summary",
            "state",
            "blocking",
            "open_decisions",
            "next",
            "warnings",
        ]
        common["properties"].update(
            {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "state": {"type": "object"},
                "blocking": {"type": "array"},
                "open_decisions": {"type": "array"},
                "next": {"type": ["object", "null"]},
                "warnings": {"type": "array"},
            }
        )
        if context == "discovery":
            common["required"] += ["resources", "pagination"]
            common["properties"].update(
                {
                    "resources": {"type": "array"},
                    "pagination": pagination,
                }
            )
            if view == "detail":
                common["required"] += [
                    "source_format",
                    "resource_versions",
                    "milestones",
                    "followups",
                    "questions",
                ]
                common["properties"].update(
                    {
                        "source_format": {"type": ["string", "null"]},
                        "resource_versions": {"type": "object"},
                        "milestones": {"type": "array"},
                        "followups": {"type": "array"},
                        "questions": {"type": "array"},
                    }
                )
        elif context == "audit" and view == "detail":
            common["required"] += ["findings", "pagination", "violations"]
            common["properties"].update(
                {
                    "findings": {"type": "array"},
                    "pagination": pagination,
                    "violations": {"type": "array"},
                }
            )
        elif context == "resource" and view == "detail":
            common["required"] += [
                "metadata",
                "relations",
                "followups",
                "questions",
            ]
            common["properties"].update(
                {
                    "metadata": {"type": "object"},
                    "relations": {"type": "object"},
                    "followups": {"type": "array"},
                    "questions": {"type": "array"},
                    "items": {"type": "array"},
                    "events": {"type": "array"},
                }
            )
    elif view == "history":
        common["required"] += ["records", "pagination"]
        common["properties"].update(
            {
                "records": {"type": "array"},
                "pagination": pagination,
            }
        )
    elif view == "version":
        pass
    elif view == "raw":
        common["required"].append("data")
        common["properties"]["data"] = {}
    elif view == "schema":
        common["required"] += [
            "schema_version",
            "response_schema",
            "response_schemas",
        ]
        common["properties"].update(
            {
                "schema_version": {"type": "integer"},
                "response_schema": {"type": "object"},
                "response_schemas": {"type": "object"},
                "storage_schema": {"type": "object"},
                "op_vocab": {"type": "object"},
                "dos_donts": {"type": "object"},
            }
        )
        if context != "audit":
            common["required"] += ["storage_schema", "op_vocab", "dos_donts"]
    return common


def _response_schemas(
    selector: ResourceSelector, *, context: str
) -> dict[str, dict[str, Any]]:
    views = ("summary", "detail", "version", "raw", "schema")
    if context != "audit":
        views = ("summary", "detail", "history", "version", "raw", "schema")
    return {view: _response_schema(selector, view, context=context) for view in views}


def resource_view(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    view: str,
    provenance: dict[str, str],
    deps: list[dict[str, Any]] | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    include_prompts: bool = False,
    storage_schema: dict[str, Any] | None = None,
    op_vocab: dict[str, Any] | None = None,
    dos_donts: dict[str, Any] | None = None,
    response_context: str = "resource",
) -> dict[str, Any]:
    """Transform one canonical resource into the requested response view."""

    selected = normalize_view(view)
    if selector.type == "review" and selected in {"summary", "detail"}:
        from reckon.serve import discover_plans

        checkout = provenance.get("checkout")
        if checkout:
            discovered = discover_plans(
                Path(checkout) / "docs", selector.project, Path(checkout) / "docs/state"
            )
            data = compose_review(
                data,
                discovered.get("inventory", []),
                discovered.get("sprints", []),
                selector.project,
                discovered,
            )
    dependencies = deps or []
    result: dict[str, Any]
    if selected == "summary":
        result = _summary(
            selector,
            version,
            data,
            deps=dependencies,
            include_prompts=include_prompts,
        )
    elif selected == "detail":
        result = _detail(
            selector,
            version,
            data,
            deps=dependencies,
            include_prompts=include_prompts,
        )
    elif selected == "history":
        page, pagination = paginate(
            _history_records(selector, data, include_prompts=include_prompts),
            cursor=cursor,
            limit=limit,
        )
        result = {
            "resource": selector.as_dict(),
            "version": version,
            "view": "history",
            "records": page,
            "pagination": pagination,
        }
    elif selected == "version":
        result = {
            "resource": selector.as_dict(),
            "version": version,
            "view": "version",
        }
    elif selected == "raw":
        result = {
            "resource": selector.as_dict(),
            "version": version,
            "view": "raw",
            "data": data,
        }
    else:
        return {
            "resource": selector.as_dict(),
            "version": version,
            "view": "schema",
            "provenance": provenance,
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "response_schema": _response_schema(
                selector, selected, context=response_context
            ),
            "response_schemas": _response_schemas(selector, context=response_context),
            "storage_schema": storage_schema or {},
            "op_vocab": op_vocab or {},
            "dos_donts": dos_donts or {},
        }

    result["provenance"] = provenance
    if (
        selector.type == "plan"
        and not selector.archived
        and selected not in {"version", "schema"}
    ):
        runs = in_flight_by_plan(selector.project).get(selector.id)
        if runs:
            result["in_flight"] = runs
    return result


def discovery_view(
    project: str,
    raw: dict[str, Any],
    *,
    view: str,
    provenance: dict[str, str],
    cursor: str | None,
    limit: int | None,
    include_prompts: bool,
    storage_schema: dict[str, Any] | None = None,
    op_vocab: dict[str, Any] | None = None,
    dos_donts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build progressive project-discovery responses."""

    selected = normalize_view(view)
    selector = ResourceSelector(project=project, type="project", id=project)
    version = int((raw.get("resource_versions") or {}).get("project:project", 0))
    if selected == "raw":
        return {
            "resource": selector.as_dict(),
            "version": version,
            "view": "raw",
            "provenance": provenance,
            "data": raw,
        }
    if selected == "schema":
        return resource_view(
            selector,
            version,
            {},
            view="schema",
            provenance=provenance,
            storage_schema=storage_schema,
            op_vocab=op_vocab,
            dos_donts=dos_donts,
            response_context="discovery",
        )
    if selected == "history":
        page, pagination = paginate(
            list(raw.get("timeline") or []), cursor=cursor, limit=limit
        )
        return {
            "resource": selector.as_dict(),
            "version": version,
            "view": "history",
            "provenance": provenance,
            "records": [{"kind": "timeline", **item} for item in page],
            "pagination": pagination,
        }
    if include_prompts and selected == "summary":
        raise ViewRequestError(
            "invalid_view_option",
            "Summary responses never include full followup prompts.",
            "Use view='detail' with include_prompts=true.",
        )
    plan_resources = [
        {
            key: item.get(key)
            for key in (
                "slug",
                "type",
                "title",
                "status",
                "effective_status",
                "impl",
            )
            if item.get(key) not in (None, "")
        }
        | ({"effort": _plan_effort(item)} if _plan_effort(item) is not None else {})
        for item in raw.get("plans") or []
        if isinstance(item, dict)
    ]
    sprint_resources = [
        {
            "id": sprint.get("id"),
            "type": "sprint",
            "title": sprint.get("theme", ""),
            "status": sprint.get("status", ""),
            "items": len(sprint.get("items") or []),
            "completed": sum(
                (item.get("effective_status") or item.get("status"))
                in {"shipped", "done"}
                for item in sprint.get("items") or []
                if isinstance(item, dict)
            ),
            "blocked": sum(
                (item.get("effective_status") or item.get("status")) == "blocked"
                for item in sprint.get("items") or []
                if isinstance(item, dict)
            ),
            "capacity": _sprint_capacity(sprint),
        }
        for sprint in raw.get("sprints") or []
        if isinstance(sprint, dict)
    ]
    resources = sprint_resources + plan_resources
    page, pagination = paginate(resources, cursor=cursor, limit=limit)
    blockers = [
        {
            key: item.get(key)
            for key in ("id", "status", "summary", "owner", "next")
            if item.get(key) not in (None, "")
        }
        for item in raw.get("blockers") or []
        if isinstance(item, dict) and item.get("status", "open") != "resolved"
    ]
    followups = [
        item
        for item in raw.get("followups") or []
        if isinstance(item, dict) and item.get("status", "open") == "open"
    ]
    result: dict[str, Any] = {
        "resource": selector.as_dict(),
        "version": version,
        "view": selected,
        "provenance": provenance,
        "title": project,
        "summary": (
            f"{raw.get('summary', {}).get('plans', 0)} plans; "
            f"{raw.get('summary', {}).get('open_followups', 0)} open followups."
        ),
        "state": {
            "active_sprint_id": raw.get("active_sprint_id"),
            "source_format": raw.get("source_format", "legacy-index"),
            **(raw.get("summary") or {}),
        },
        "blocking": blockers,
        "open_decisions": [],
        "next": (
            _followup_record(followups[0], include_prompts=False) if followups else None
        ),
        "warnings": [],
        "resources": page,
        "pagination": pagination,
    }
    if isinstance(raw.get("review"), dict):
        result["review"] = raw["review"]
    if selected == "detail":
        result["source_format"] = raw.get("source_format")
        result["resource_versions"] = raw.get("resource_versions") or {}
        result["milestones"] = list(raw.get("milestones") or [])
        result["followups"] = [
            _followup_record(item, include_prompts=include_prompts)
            for item in followups
        ]
        result["questions"] = list(raw.get("questions") or [])
    return result


def audit_view(
    project: str,
    raw: dict[str, Any],
    *,
    view: str,
    cursor: str | None,
    limit: int | None,
) -> dict[str, Any]:
    """Build progressive audit responses while preserving raw opt-in."""

    selected = normalize_view(view)
    selector = ResourceSelector(project=project, type="audit", id=project)
    if selected == "raw":
        return {
            "resource": selector.as_dict(),
            "version": 0,
            "view": "raw",
            "data": raw,
        }
    if selected == "schema":
        schemas = _response_schemas(selector, context="audit")
        return {
            "resource": selector.as_dict(),
            "version": 0,
            "view": "schema",
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "response_schema": schemas["schema"],
            "response_schemas": schemas,
        }
    findings = list(raw.get("findings") or [])
    counts = raw.get("finding_counts") or {
        "total": len(findings),
        "by_severity": {},
        "by_category": {},
        "by_code": {},
    }
    errors = [
        {
            key: item.get(key)
            for key in ("category", "code", "message", "slug", "path")
            if item.get(key) not in (None, "")
        }
        for item in findings
        if item.get("severity") == "error"
    ]
    summary = {
        "resource": selector.as_dict(),
        "version": 0,
        "view": selected,
        "title": f"{project} audit",
        "summary": (
            f"{raw.get('conformant', 0)}/{raw.get('checked', 0)} conformant; "
            f"{counts.get('total', len(findings))} findings."
        ),
        "state": {
            "checked": raw.get("checked", 0),
            "conformant": raw.get("conformant", 0),
            "finding_counts": counts,
        },
        "blocking": errors,
        "open_decisions": [],
        "next": None,
        "warnings": [],
    }
    if selected == "summary":
        return summary
    if selected == "history":
        raise ViewRequestError(
            "invalid_view",
            "Audit resources do not have a history view.",
            "Use summary, detail, raw, or schema.",
        )
    page, pagination = paginate(findings, cursor=cursor, limit=limit)
    summary["findings"] = page
    summary["pagination"] = pagination
    summary["violations"] = list(raw.get("violations") or [])
    return summary


def _roadmap_finding_counts(findings: list[dict[str, Any]]) -> dict[str, Any]:
    by_severity: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity") or "unknown")
        by_severity[severity] = by_severity.get(severity, 0) + 1
    return {"total": len(findings), "by_severity": by_severity}


def _roadmap_project_summary(raw: dict[str, Any]) -> dict[str, Any]:
    findings = [
        item for item in raw.get("wiring_findings") or [] if isinstance(item, dict)
    ]
    completion = raw.get("completion") or {}
    schedule = raw.get("schedule") or {}
    dependency_ready = len(raw.get("ready_now") or [])
    dependency_blocked = len(raw.get("blocked") or [])
    dependency_deferred = len(raw.get("deferred") or [])
    return {
        "project": raw.get("project", ""),
        "completion": {
            key: completion.get(key, 0)
            for key in (
                "plans",
                "completed",
                "pending",
                "lifecycle_completion_pct",
                "implementation_pct",
            )
        },
        "ready": dependency_ready,
        "blocked": dependency_blocked,
        "deferred": dependency_deferred,
        "dependency_readiness": {
            "ready": dependency_ready,
            "blocked": dependency_blocked,
            "deferred": dependency_deferred,
        },
        "schedule_readiness": {
            key: schedule.get(key)
            for key in (
                "configured",
                "configuration_key",
                "window_sprints",
                "horizon_depth",
                "open_sprints",
                "earliest_open_sprint",
                "ready_sprints",
                "ready",
                "deferred",
            )
        },
        "finding_counts": _roadmap_finding_counts(findings),
    }


def roadmap_view(
    raw: dict[str, Any],
    *,
    view: str,
    cursor: str | None,
    limit: int | None,
) -> dict[str, Any]:
    """Build compact, paginated roadmap views while preserving raw opt-in."""

    selected = normalize_view(view)
    if selected not in {"summary", "detail", "raw"}:
        raise ViewRequestError(
            "invalid_view",
            "Roadmaps support summary, detail, or raw views.",
            "Use summary for counts, detail for paginated findings, or raw.",
        )
    project = str(raw.get("project") or "")
    if selected == "raw":
        return {"project": project, "view": "raw", "data": raw}

    if project == "*":
        reports = [item for item in raw.get("projects") or [] if isinstance(item, dict)]
        projects = [_roadmap_project_summary(report) for report in reports]
        result: dict[str, Any] = {
            "project": "*",
            "view": selected,
            "portfolio": raw.get("portfolio") or {},
            "projects": projects,
        }
        if selected == "summary":
            return result
        findings = [
            {"project": report.get("project", ""), **finding}
            for report in reports
            for finding in report.get("wiring_findings") or []
            if isinstance(finding, dict)
        ]
        page, pagination = paginate(findings, cursor=cursor, limit=limit)
        result["findings"] = page
        result["finding_counts"] = _roadmap_finding_counts(findings)
        result["pagination"] = pagination
        return result

    if selected == "summary":
        return {
            "project": project,
            "view": "summary",
            **_roadmap_project_summary(raw),
            "critical_path": raw.get("critical_path") or {},
        }

    findings = [
        item for item in raw.get("wiring_findings") or [] if isinstance(item, dict)
    ]
    page, pagination = paginate(findings, cursor=cursor, limit=limit)
    result = dict(raw)
    result["view"] = "detail"
    result["wiring_findings"] = page
    result["finding_counts"] = _roadmap_finding_counts(findings)
    result["pagination"] = pagination
    return result
