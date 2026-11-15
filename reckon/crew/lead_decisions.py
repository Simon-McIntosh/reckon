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

from reckon import _store
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
    try:
        values, labels = _store._decision_options_as_choices(value)
    except _store.OpError as exc:
        raise ValueError(f"options: {exc}") from exc
    options = [
        {"value": item.strip(), "label": labels[item].strip()} for item in values
    ]
    if any(not all(option.values()) for option in options):
        raise ValueError("options must carry non-empty value and label")
    values = [option["value"] for option in options]
    if len(set(values)) != len(values):
        raise ValueError("options must have distinct values")
    if strict:
        if not 2 <= len(options) <= 4:
            raise ValueError("options must contain two to four choices")
        if not recommended or recommended not in values:
            raise ValueError("recommended must name one of the options")
    return sorted(options, key=lambda option: option["value"] != recommended)


def _pending_reference(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("pending_on must be an object naming a run_id or gate_id")
    if not value:
        return {}
    if len(value) != 1 or not set(value) <= {"run_id", "gate_id"}:
        raise ValueError("pending_on must name exactly one run_id or gate_id")
    key, identifier = next(iter(value.items()))
    if (
        not isinstance(identifier, str)
        or not identifier.strip()
        or Path(identifier).name != identifier
        or identifier in {".", ".."}
    ):
        raise ValueError("pending_on must name a non-empty run_id or gate_id")
    return {key: identifier}


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
    recommended = str(record.get("recommended") or "").strip()
    options = _options(record.get("options"), recommended, strict=True)
    choice = str(record.get("choice") or "").strip()
    reversible = record.get("reversible", False)
    if not isinstance(reversible, bool):
        raise TypeError("reversible must be a boolean")
    standing_rule = str(record.get("standing_rule") or "").strip()
    if choice and not reversible:
        raise ValueError("choice requires a reversible decision under a standing_rule")
    if reversible and (not choice or not standing_rule):
        raise ValueError("reversible requires a choice and standing_rule")
    if choice and choice not in {option["value"] for option in options}:
        raise ValueError("choice must name one of the options")
    provenance = str(record.get("recommendation_source") or "").strip()
    if provenance not in {"", "owner", "reviewer", "unknown"}:
        raise ValueError("recommendation_source must be owner, reviewer or unknown")
    data.update(
        options=options,
        recommended=recommended,
        recommended_by=str(record.get("recommended_by") or data["session"]).strip(),
        recommendation_source=provenance,
        risk=str(record.get("risk") or "").strip(),
        lead_action=str(record.get("lead_action") or "").strip(),
        held_requests=_names(record.get("held_requests", []), "held_requests"),
        held_runs=_names(record.get("held_runs", []), "held_runs"),
        held_sections=_names(record.get("held_sections", []), "held_sections"),
        decision_key=str(record.get("decision_key") or "").strip(),
        pending_on=_pending_reference(record.get("pending_on", {})),
        reversible=reversible,
        standing_rule=standing_rule,
        choice=choice,
        source="crew",
    )
    return data


def record_decision(record: Mapping[str, Any]) -> dict[str, Any]:
    """Record a ready, pending, or taken-and-reversible question for the lead.

    The caller supplies question, context, options (value/label pairs),
    recommended, project, session, plan, section, held_requests and
    held_sections. Optional release context is held_runs, risk and lead_action;
    recommended_by and recommendation_source preserve the author's provenance.
    pending_on names a run_id or gate_id whose report makes the question ready.
    A taken choice requires reversible=True and its standing_rule; recording
    such a choice leaves it open for review rather than answering it as the lead.
    decision_key links the question to an authored plan decision, whose direct
    answer then also closes this question. Identity and time are supplied here.
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


def _plan_decisions(
    project: str, docs: Path, states: dict[tuple[str, str], Any]
) -> list[dict[str, Any]]:
    rows = []
    for resource in iter_resources(docs, project, include_archived=False):
        if resource.type != "plan":
            continue
        state = read_state_file(resource.path)
        states[(project, resource.slug)] = state
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
                    "recommended_by": authored.get("recommended_by") or "",
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


def _report_received(row: Mapping[str, Any], gates: list[dict[str, Any]]) -> bool:
    """A named run or gate needs a receipt, not an absent pointer or process."""
    from reckon import ledger
    from reckon.crew.recovery_liveness import _run_exit_record

    pending = row.get("pending_on") or {}
    if not pending:
        return True
    if "run_id" in pending:
        run_id = pending["run_id"]
        receipt = _run_exit_record({"run_id": run_id}, require_run_id=True)
        if receipt and (
            receipt.get("exit_code") is not None or receipt.get("signal") is not None
        ):
            return True
        path = ledger.run_path(row["project"], run_id)
        if not path.is_file():
            return False
        completed = ledger._read_run(path)
        return completed.get("run_id") == run_id and bool(completed.get("completed_at"))
    return any(
        gate.get("id") == pending["gate_id"]
        and gate.get("project") == row["project"]
        and gate.get("ended_at")
        and gate.get("exit_status") is not None
        for gate in gates
    )


def _answered_in_plan(
    row: Mapping[str, Any], states: Mapping[tuple[str, str], Any]
) -> bool:
    key = row.get("decision_key")
    if not key:
        return False
    state = states.get((row["project"], row["plan"])) or {}
    decision = (state.get("decisions") or {}).get(key) or {}
    return bool(str(decision.get("choice") or "").strip())


def read_decisions(project: str | None = None) -> dict[str, Any]:
    """Return ready, pending and taken-and-reversible questions, ranked by work.

    Plan ownership and liveness come from the existing coordinator resolver.
    Missing historical authorship and recording time stay explicitly null.
    A corrupt crew record refuses the read rather than reporting no decision.
    """
    from reckon.crew import gate_watch, plan_coordinator

    mounts = mounted_project_docs()
    if project and project != "*":
        if project not in mounts:
            raise ValueError(f"project {project!r} is not mounted")
        mounts = {project: mounts[project]}
    rows = []
    for path in sorted((crew_home() / "decisions").glob("*.json")):
        try:
            record = _read_record(path)
            if record["project"] in mounts and (
                not record.get("choice") or record.get("reversible")
            ):
                rows.append({**record, "source": "crew"})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"cannot read decision {path}: {exc}") from exc
    states: dict[tuple[str, str], Any] = {}
    for owner, docs in sorted(mounts.items()):
        rows.extend(_plan_decisions(owner, docs, states))
    all_pointers = list_live()
    pointers = [
        pointer for pointer in all_pointers if pointer.get("phase") in WAITING_PHASES
    ]
    ownership = {
        owner: plan_coordinator.resolve_plan_coordinators(
            owner,
            root=docs.parent,
            pointers=[
                pointer for pointer in all_pointers if pointer.get("project") == owner
            ],
        )["plans"]
        for owner, docs in mounts.items()
        if any(row["project"] == owner for row in rows)
    }
    rows = [row for row in rows if not _answered_in_plan(row, states)]
    gates = (
        gate_watch.read_document()["gates"]
        if any("gate_id" in (row.get("pending_on") or {}) for row in rows)
        else []
    )
    for row in rows:
        row.setdefault("held_runs", [])
        row.setdefault("risk", "")
        row.setdefault("lead_action", "")
        row.setdefault("pending_on", {})
        row.setdefault("reversible", False)
        row.setdefault("standing_rule", "")
        row.setdefault("recommended_by", "")
        held = _held_requests(row, pointers)
        row["held_requests"] = sorted({str(pointer["run_id"]) for pointer in held})
        row["held_request_count"] = len(row["held_requests"])
        row["held_work_count"] = len(
            set(row["held_requests"]) | set(row["held_runs"])
        ) + len(row["held_sections"])
        owner = ownership.get(row["project"], {}).get(row["plan"], {})
        row["owner_session"] = owner.get("coordinator")
        row["plan_owner"] = owner.get("plan_owner") or ""
        row["plan_has_live_session"] = bool(owner.get("live"))
        if not row.get("recommendation_source"):
            by = row["recommended_by"]
            row["recommendation_source"] = (
                "owner"
                if by and by in {row["owner_session"], row["plan_owner"]}
                else "reviewer"
                if by
                else "unknown"
            )
        if row["source"] == "plan":
            sessions = {
                str(pointer["session"]) for pointer in held if pointer.get("session")
            }
            row["session"] = row["owner_session"] or (
                next(iter(sessions)) if len(sessions) == 1 else None
            )
            row["sessions"] = sorted(sessions)
        row["state"] = (
            "taken-and-reversible"
            if row["choice"] and row["reversible"]
            else "ready"
            if _report_received(row, gates)
            else "pending"
        )
    rows.sort(
        key=lambda row: (
            -row["held_work_count"],
            row.get("recorded_at") or "",
            row["project"],
            row["id"],
        )
    )
    ready = [row for row in rows if row["state"] == "ready"]
    pending = [row for row in rows if row["state"] == "pending"]
    reversible = [row for row in rows if row["state"] == "taken-and-reversible"]
    return {
        "ok": True,
        "view": "decisions",
        "projects": sorted(mounts),
        "count": len(ready),
        "total_count": len(rows),
        "decisions": ready,
        "pending": pending,
        "taken_and_reversible": reversible,
    }
