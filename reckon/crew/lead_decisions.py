"""Durable questions for the lead, joined with unanswered plan decisions.

Records live under the shared crew home so they survive their owning session.
The read includes only mounted projects and ranks the work still waiting,
rather than requests that have launched or disappeared since recording.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from reckon._plan_html import read_state_file
from reckon._store import write_json_atomically
from reckon.crew.queue_edit import WAITING_PHASES
from reckon.crew.run_paths import crew_home
from reckon.crew.runs import list_live
from reckon.flight import mounted_project_docs
from reckon.lifecycle import TERMINAL_STATUSES
from reckon.resources import iter_resources
from reckon.roadmap import unsettled_decisions


def decision_path(decision_id: str) -> Path:
    """Resolve a crew decision identity without allowing path traversal."""
    if not decision_id or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in decision_id
    ):
        raise ValueError(
            "decision id must contain lowercase letters, digits or hyphens"
        )
    return crew_home() / "decisions" / f"{decision_id}.json"


def _names(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty names")
    return sorted({item.strip() for item in value})


def _options(value: Any, recommended: str, *, strict: bool) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise TypeError("options must be a list")
    options = []
    for option in value:
        if not isinstance(option, Mapping):
            raise TypeError("options must carry value and label")
        entry = {key: str(option.get(key) or "").strip() for key in ("value", "label")}
        if not all(entry.values()):
            raise ValueError("options must carry non-empty value and label")
        options.append(entry)
    values = [option["value"] for option in options]
    if len(set(values)) != len(values):
        raise ValueError("options must have distinct values")
    if strict:
        if not 2 <= len(options) <= 4:
            raise ValueError("options must contain two to four choices")
        if not recommended or recommended not in values:
            raise ValueError("recommended must name one of the options")
    return sorted(options, key=lambda option: option["value"] != recommended)


def validate_decision(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise an open question before any hold or write."""
    if not isinstance(record, Mapping):
        raise TypeError("record must be a JSON object")
    required = ("question", "context", "project", "session", "plan", "section")
    data = {key: str(record.get(key) or "").strip() for key in required}
    for key, value in data.items():
        if not value:
            raise ValueError(f"{key} is required")
    if data["project"] not in mounted_project_docs():
        raise ValueError("project must name a mounted project")
    if record.get("choice"):
        raise ValueError("choice cannot be set when recording an open decision")
    recommended = str(record.get("recommended") or "").strip()
    data.update(
        options=_options(record.get("options"), recommended, strict=True),
        recommended=recommended,
        held_requests=_names(record.get("held_requests", []), "held_requests"),
        held_sections=_names(record.get("held_sections", []), "held_sections"),
        choice="",
        source="crew",
    )
    return data


def record_decision(record: Mapping[str, Any]) -> dict[str, Any]:
    """Create an open decision; answering is a separate, authorised operation.

    The caller supplies question, context, options (value/label pairs),
    recommended, project, session, plan, section, held_requests and
    held_sections. Identity, recording time and the empty choice are supplied
    here, so recording cannot also answer a question or overwrite another one.
    """
    data = {
        **validate_decision(record),
        "id": str(uuid4()),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    write_json_atomically(decision_path(data["id"]), data, fsync_directory=True)
    return data


def _request_identity(pointer: Mapping[str, Any]) -> tuple[str, str]:
    node = pointer.get("node") or {}
    options = pointer.get("dispatch_options") or {}
    return (
        str(pointer.get("plan") or node.get("plan") or options.get("plan_slug") or ""),
        str(pointer.get("section") or node.get("section") or "").removeprefix("#"),
    )


def _held_requests(
    decision: Mapping[str, Any], pointers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    named = set(decision.get("held_requests") or [])
    matches = []
    for pointer in pointers:
        if pointer.get("project") != decision["project"]:
            continue
        plan, section = _request_identity(pointer)
        hold = pointer.get("queue_hold") or {}
        bound = (hold.get("decision_id") or pointer.get("decision_id")) == decision[
            "id"
        ]
        scoped = (
            decision["source"] == "plan"
            and plan == decision["plan"]
            and (not decision["held_sections"] or section in decision["held_sections"])
        )
        if str(pointer.get("run_id") or "") in named or bound or scoped:
            matches.append(pointer)
    return matches


def _plan_decisions(project: str, docs: Path) -> list[dict[str, Any]]:
    rows = []
    for resource in iter_resources(docs, project, include_archived=False):
        if resource.type != "plan":
            continue
        state = read_state_file(resource.path)
        if state.get("status") in TERMINAL_STATUSES:
            continue
        state = {**state, "slug": resource.slug, "project": project}
        for wait in unsettled_decisions(state, waiting_context=True):
            key = wait["id"]
            authored = state["decisions"][key]
            recommended = str(authored.get("recommended") or "")
            rows.append(
                {
                    "id": f"plan:{project}:{resource.slug}:{key}",
                    "key": key,
                    "source": "plan",
                    "project": project,
                    "session": None,
                    "plan": resource.slug,
                    "section": wait["sections"][0] if wait["sections"] else None,
                    "question": wait["question"],
                    "context": authored.get("context") or "",
                    "options": _options(
                        [
                            {
                                "value": value,
                                "label": authored["option_labels"].get(value, value),
                            }
                            for value in authored.get("choices") or []
                        ],
                        recommended,
                        strict=False,
                    ),
                    "recommended": recommended,
                    "choice": "",
                    # An unanswered plan decision stores no recording time or
                    # owning session. A plan edit date cannot stand in for it.
                    "recorded_at": None,
                    "plan_modified": state.get("modified"),
                    "held_requests": [],
                    "held_sections": wait["sections"],
                    "holds_whole_plan": wait["holds_whole_plan"],
                }
            )
    return rows


def _read_record(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise TypeError("expected a decision object")
    for key in (
        "id",
        "project",
        "session",
        "plan",
        "section",
        "question",
        "context",
        "options",
        "recommended",
        "recorded_at",
        "choice",
        "held_requests",
        "held_sections",
    ):
        if key not in record:
            raise ValueError(f"decision is missing {key}")
    return record


def read_decisions(project: str | None = None) -> dict[str, Any]:
    """Return all mounted open decisions, most held requests and sections first.

    Missing authorship fields on plan-only decisions are explicitly null. The
    holding pointers supply an owning session only when they agree on one.
    A corrupt crew record refuses the read rather than reporting no decision.
    """
    mounts = mounted_project_docs()
    if project and project != "*":
        if project not in mounts:
            raise ValueError(f"project {project!r} is not mounted")
        mounts = {project: mounts[project]}
    rows = []
    for path in sorted((crew_home() / "decisions").glob("*.json")):
        try:
            record = _read_record(path)
            if record["project"] in mounts and not record.get("choice"):
                rows.append({**record, "source": "crew"})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"cannot read decision {path}: {exc}") from exc
    for owner, docs in sorted(mounts.items()):
        rows.extend(_plan_decisions(owner, docs))
    pointers = [
        pointer for pointer in list_live() if pointer.get("phase") in WAITING_PHASES
    ]
    for row in rows:
        held = _held_requests(row, pointers)
        row["held_requests"] = sorted({str(pointer["run_id"]) for pointer in held})
        row["held_request_count"] = len(row["held_requests"])
        row["held_work_count"] = row["held_request_count"] + len(row["held_sections"])
        if row["source"] == "plan":
            sessions = {
                str(pointer["session"]) for pointer in held if pointer.get("session")
            }
            row["session"] = next(iter(sessions)) if len(sessions) == 1 else None
            row["sessions"] = sorted(sessions)
    rows.sort(
        key=lambda row: (
            -row["held_work_count"],
            row.get("recorded_at") or "",
            row["project"],
            row["id"],
        )
    )
    return {
        "ok": True,
        "view": "decisions",
        "projects": sorted(mounts),
        "count": len(rows),
        "decisions": rows,
    }
