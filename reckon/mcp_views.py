"""Progressive, human-readable response views for Reckon's MCP tools."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from reckon.lifecycle import effective_status, unresolved_dependencies

VIEW_NAMES = frozenset({"summary", "detail", "history", "raw", "schema"})
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
        "audit",
    }
)
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
RESPONSE_SCHEMA_VERSION = 1
MAX_SELECTOR_LENGTH = 128
MAX_CURSOR_LENGTH = 256
MAX_ERROR_TEXT_LENGTH = 512
MAX_ERROR_COLLECTION_ITEMS = 25
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    return result


def _state(
    selector: ResourceSelector,
    data: dict[str, Any],
    blocking: list[Any],
) -> dict[str, Any]:
    resource_type = selector.type
    if resource_type == "plan":
        workflow_status = str(data.get("status") or "draft")
        return {
            "status": workflow_status,
            "effective_status": effective_status(workflow_status, blocking),
            "progress": float(data.get("impl", 0.0) or 0.0),
            "sprint": data.get("sprint") or None,
            "milestone": data.get("milestone") or None,
            "capability": data.get("capability") or {},
        }
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
            "effort",
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
            for item in data.get("items") or []
            if isinstance(item, dict)
        ]
    elif selector.type == "timeline":
        result["events"] = list(data.get("events") or [])
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
    views = ("summary", "detail", "raw", "schema")
    if context != "audit":
        views = ("summary", "detail", "history", "raw", "schema")
    return {view: _response_schema(selector, view, context=context) for view in views}


def resource_view(
    selector: ResourceSelector,
    version: int,
    data: dict[str, Any],
    *,
    view: str,
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
    dependencies = deps or []
    if selected == "summary":
        return _summary(
            selector,
            version,
            data,
            deps=dependencies,
            include_prompts=include_prompts,
        )
    if selected == "detail":
        return _detail(
            selector,
            version,
            data,
            deps=dependencies,
            include_prompts=include_prompts,
        )
    if selected == "history":
        page, pagination = paginate(
            _history_records(selector, data, include_prompts=include_prompts),
            cursor=cursor,
            limit=limit,
        )
        return {
            "resource": selector.as_dict(),
            "version": version,
            "view": "history",
            "records": page,
            "pagination": pagination,
        }
    if selected == "raw":
        return {
            "resource": selector.as_dict(),
            "version": version,
            "view": "raw",
            "data": data,
        }
    return {
        "resource": selector.as_dict(),
        "version": version,
        "view": "schema",
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "response_schema": _response_schema(
            selector, selected, context=response_context
        ),
        "response_schemas": _response_schemas(selector, context=response_context),
        "storage_schema": storage_schema or {},
        "op_vocab": op_vocab or {},
        "dos_donts": dos_donts or {},
    }


def discovery_view(
    project: str,
    raw: dict[str, Any],
    *,
    view: str,
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
            "data": raw,
        }
    if selected == "schema":
        return resource_view(
            selector,
            version,
            {},
            view="schema",
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
