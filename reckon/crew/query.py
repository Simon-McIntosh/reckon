"""Compact read models joining live pointers with committed run records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon.crew.node import normalize_section
from reckon.crew.recovery import classify_pointer
from reckon.crew.runs import list_live

DEFAULT_RUN_FIELDS = (
    "run_id",
    "node",
    "plan",
    "section",
    "source",
    "classification",
    "process_alive",
    "session_id",
    "session_id_source",
    "worktree",
    "worktree_exists",
    "transcript_path",
    "transcript_exists",
    "resumable",
    "resumable_reason",
)

OPTIONAL_RUN_FIELDS = (
    "member",
    "agent",
    "base_sha",
    "manifest_present",
    "commits",
)

RUN_SOURCES = frozenset({"all", "ledger", "live"})


class RunQueryError(ValueError):
    """A runs-view request cannot be represented by the compact read model."""


def _path_exists(value: Any, *, directory: bool = False) -> bool:
    """Report whether a non-empty path exists with the requested kind."""
    text = str(value or "").strip()
    if not text:
        return False
    path = Path(text)
    return path.is_dir() if directory else path.is_file()


def _selected_fields(fields: Iterable[str] | None) -> tuple[str, ...]:
    """Return default fields plus validated opt-in fields, in stable order."""
    if fields is None:
        return DEFAULT_RUN_FIELDS
    if isinstance(fields, str):
        requested = [part.strip() for part in fields.split(",") if part.strip()]
    else:
        requested = [str(field).strip() for field in fields if str(field).strip()]
    known = set(DEFAULT_RUN_FIELDS) | set(OPTIONAL_RUN_FIELDS)
    unknown = sorted(set(requested) - known)
    if unknown:
        raise RunQueryError(
            "unknown runs fields "
            + ", ".join(repr(field) for field in unknown)
            + "; optional fields are "
            + ", ".join(OPTIONAL_RUN_FIELDS)
        )
    extras = tuple(field for field in OPTIONAL_RUN_FIELDS if field in requested)
    return (*DEFAULT_RUN_FIELDS, *extras)


def _session_fields(record: Mapping[str, Any], source: str) -> tuple[Any, Any]:
    """Preserve directly recorded session evidence and name its source."""
    session_id = record.get("session_id") or None
    session_source = record.get("session_id_source") or None
    if session_id is not None and session_source is None:
        session_source = "pointer" if source == "live" else "ledger"
    return session_id, session_source


def _compact_row(
    record: Mapping[str, Any],
    *,
    source: str,
    selected_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Project one source record into the stable compact row shape."""
    classified = classify_pointer(record) if source == "live" else {}
    node_data = record.get("node")
    node_mapping = node_data if isinstance(node_data, Mapping) else {}
    node = classified.get("node") if source == "live" else record.get("node")
    plan = classified.get("plan") if source == "live" else record.get("plan")
    section = node_mapping.get("section") if source == "live" else record.get("section")
    worktree = record.get("worktree") or None
    transcript = record.get("transcript_path") or None
    session_id, session_id_source = _session_fields(record, source)
    if source == "live":
        commits = classified.get("manifest_commits") or record.get("commits") or []
        manifest_present = classified.get("manifest_present", False)
    else:
        commits = record.get("commits") or []
        manifest_present = _path_exists(record.get("manifest_path"))
    complete = {
        "run_id": str(record.get("run_id") or ""),
        "node": str(node or ""),
        "plan": str(plan or ""),
        "section": normalize_section(section) if section else "",
        "source": source,
        "classification": (
            classified.get("classification")
            if source == "live"
            else record.get("classification")
        ),
        "process_alive": (
            classified.get("process_alive")
            if source == "live"
            else record.get("process_alive")
        ),
        "session_id": session_id,
        "session_id_source": session_id_source,
        "worktree": worktree,
        "worktree_exists": (
            bool(record.get("worktree_exists"))
            if "worktree_exists" in record
            else _path_exists(worktree, directory=True)
        ),
        "transcript_path": transcript,
        "transcript_exists": (
            bool(record.get("transcript_exists"))
            if "transcript_exists" in record
            else _path_exists(transcript)
        ),
        "resumable": record.get("resumable"),
        "resumable_reason": record.get("resumable_reason") or None,
        "member": str(record.get("member") or ""),
        "agent": (
            dict(record["agent"]) if isinstance(record.get("agent"), Mapping) else {}
        ),
        "base_sha": str(record.get("base_sha") or ""),
        "manifest_present": bool(manifest_present),
        "commits": [str(commit) for commit in commits],
    }
    return {field: complete[field] for field in selected_fields}


def _matches(
    row: Mapping[str, Any],
    *,
    node: str | None,
    plan: str | None,
    section: str | None,
    session: str | None,
    member: str | None,
    classification: str | None,
    resumable: bool | None,
) -> bool:
    """Apply every runs-view filter to one normalized row."""
    string_filters = {
        "node": node,
        "plan": plan,
        "session_id": session,
        "member": member,
        "classification": classification,
    }
    if any(
        expected is not None and str(row.get(field) or "") != str(expected)
        for field, expected in string_filters.items()
    ):
        return False
    if section is not None and str(row.get("section") or "") != normalize_section(
        section
    ):
        return False
    return resumable is None or row.get("resumable") is resumable


def runs_view(
    project: str,
    *,
    checkout_path: str | None = None,
    source: str = "all",
    node: str | None = None,
    plan: str | None = None,
    section: str | None = None,
    session: str | None = None,
    member: str | None = None,
    classification: str | None = None,
    resumable: bool | None = None,
    newest_per_node: bool = False,
    fields: Iterable[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return compact live and committed run rows, newest first."""
    selected_source = str(source or "all").strip().lower()
    if selected_source not in RUN_SOURCES:
        raise RunQueryError(
            f"runs source must be one of {', '.join(sorted(RUN_SOURCES))}"
        )
    if resumable is not None and not isinstance(resumable, bool):
        raise RunQueryError("resumable must be true, false or omitted")
    if not isinstance(newest_per_node, bool):
        raise RunQueryError("newest_per_node must be true or false")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise RunQueryError("runs limit must be a positive integer")

    selected_fields = _selected_fields(fields)
    row_fields = tuple(dict.fromkeys((*selected_fields, "member")))
    rows: list[dict[str, Any]] = []
    if selected_source in {"all", "live"}:
        rows.extend(
            _compact_row(record, source="live", selected_fields=row_fields)
            for record in list_live()
            if str(record.get("project") or "") == project
        )
    if selected_source in {"all", "ledger"}:
        rows.extend(
            _compact_row(record, source="ledger", selected_fields=row_fields)
            for record in ledger.runs(project, checkout_path)
        )

    rows = [
        row
        for row in rows
        if _matches(
            row,
            node=node,
            plan=plan,
            section=section,
            session=session,
            member=member,
            classification=classification,
            resumable=resumable,
        )
    ]
    rows.sort(key=lambda row: str(row.get("run_id") or ""), reverse=True)
    if newest_per_node:
        newest: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            node_id = str(row.get("node") or "")
            if node_id in seen:
                continue
            seen.add(node_id)
            newest.append(row)
        rows = newest
    if limit is not None:
        rows = rows[:limit]
    if "member" not in selected_fields:
        for row in rows:
            row.pop("member", None)
    return {
        "ok": True,
        "project": project,
        "view": "runs",
        "source": selected_source,
        "count": len(rows),
        "rows": rows,
    }
