"""reckon MCP server — stdio entrypoint.

Registers all reckon.* tools and delegates IO to _store.py.

Version-write contract mirrors POST /state/<project>/<doc> in
~/Code/reckon/reckon/serve.py. Both write to the same state files at
~/docs-server/state/<project>/<slug>.json (symlinked from each project repo's
docs/state/<project>/<slug>.json). The docs-server owns the browser UI; this
MCP server owns the agent IO path. They coexist safely because both use atomic
rename and the same _version optimistic-concurrency field.

Usage (stdio, the Claude Code default):
    reckon mcp
    # or:
    python -m reckon.mcp

TODO: add SSE transport once the mcp SDK's SSE server helper stabilises.
See open decision 'transport' in the reckon-mcp-plan state JSON.

SDK note: this file uses the FastMCP pattern from mcp >= 1.0.0:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("reckon")
    @mcp.tool()
    def my_tool(...): ...
    mcp.run()

If the installed mcp SDK uses a different import path, adjust the imports
below. The tool logic (delegating to _store.py) is SDK-agnostic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# ── SDK import — adjust if mcp package layout differs ─────────────────────
try:
    from mcp.server.fastmcp import FastMCP
    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False
    FastMCP = None  # type: ignore[assignment,misc]

from reckon._store import (
    VersionConflict,
    append_to_list,
    patch_plan,
    read_plan,
    resolve_in_list,
    set_nested,
    write_plan,
)

# ── Server instance ────────────────────────────────────────────────────────

if _HAS_MCP and FastMCP is not None:
    mcp = FastMCP(
        "reckon",
        instructions=(
            "Read and write reckon plan state. "
            "Always call reckon.read_plan first to get the current version "
            "before any write tool — writes are rejected if expected_version "
            "doesn't match the file's current _version."
        ),
    )
else:
    mcp = None  # type: ignore[assignment]


def _conflict_response(exc: VersionConflict) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "version_conflict",
        "expected_version": exc.expected,
        "current_version": exc.current,
        "hint": "Re-read the plan with reckon.read_plan to get the current version, then retry.",
    }


# ── Tool definitions ───────────────────────────────────────────────────────
# Each tool is decorated with @mcp.tool() when the SDK is available.
# The plain functions are usable independently for testing.

def _read_plan(project: str, slug: str) -> dict[str, Any]:
    """Return the full data blob for one plan, plus its current version.

    Returns { project, slug, version, data }.
    """
    data, version = read_plan(project, slug)
    return {"project": project, "slug": slug, "version": version, "data": data}


def _list_plans(project: str, status: str | None = None) -> dict[str, Any]:
    """Return a lightweight index of plans for the project.

    Reads docs/state/<project>/index.json if present.
    Each entry: { slug, title, status, impl, ms, sprint, roi, effort }.

    If status is given, filters to only plans matching that status value.
    """
    data, _ = read_plan(project, "index")
    inventory = data.get("inventory", [])
    if status:
        inventory = [p for p in inventory if p.get("status") == status]
    return {
        "project": project,
        "plans": [
            {
                "slug": p.get("slug"),
                "title": p.get("title"),
                "status": p.get("status"),
                "impl": p.get("impl"),
                "ms": p.get("ms"),
                "sprint": p.get("sprint"),
                "roi": p.get("roi"),
                "effort": p.get("effort"),
            }
            for p in inventory
        ],
    }


def _patch_plan(
    project: str,
    slug: str,
    patch: dict[str, Any],
    expected_version: int,
) -> dict[str, Any]:
    """Apply a JSON merge-patch to the plan's data blob.

    Only top-level keys are merged. For nested fields (decisions, followups),
    use the dedicated tools (reckon.lock_decision, reckon.append_followup, etc.).

    Returns { ok, project, slug, new_version } or a version_conflict error.
    """
    try:
        new_version = patch_plan(project, slug, patch, expected_version)
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)


def _append_comment(
    project: str,
    slug: str,
    section_id: str,
    body: str,
    author: str,
    expected_version: int,
    quote: str | None = None,
) -> dict[str, Any]:
    """Append a comment to data.notes (keyed by section_id).

    comment shape: { id, section_id, who, bot, when, body, quote? }
    """
    cur_data, cur_version = read_plan(project, slug)
    if expected_version != cur_version:
        return _conflict_response(VersionConflict(expected_version, cur_version, cur_data))

    notes = list(cur_data.get("notes", []))
    note_id = f"n{len(notes) + 1}"
    note: dict[str, Any] = {
        "id": note_id,
        "section_id": section_id,
        "who": author,
        "bot": True,
        "when": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "body": body,
    }
    if quote:
        note["quote"] = quote
    notes.append(note)

    try:
        new_version = write_plan(project, slug, {**cur_data, "notes": notes}, cur_version)
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version, "note_id": note_id}
    except VersionConflict as e:
        return _conflict_response(e)


def _lock_decision(
    project: str,
    slug: str,
    key: str,
    choice: str,
    rationale: str,
    by: str,
    expected_version: int,
) -> dict[str, Any]:
    """Write data.decisions[key] = { choice, rationale, when, by }.

    Locks the decision in place. To reopen a locked decision, use the
    /reckon-edit --reopen dissent flow described in AGENTS.md.
    """
    decision = {
        "choice": choice,
        "rationale": rationale,
        "when": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "by": by,
    }
    try:
        new_version = set_nested(project, slug, "decisions", key, decision, expected_version)
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)


def _append_followup(
    project: str,
    slug: str,
    followup: dict[str, Any],
    expected_version: int,
) -> dict[str, Any]:
    """Append a followup record to data.followups.

    The followup dict must include: id, written_by, written_at, title, body, prompt.
    The prompt field must contain a copy-paste agent prompt per the §05 template.
    A followup without a prompt is a hard failure per AGENTS.md.
    """
    required = {"id", "written_by", "written_at", "title", "body", "prompt"}
    missing = required - set(followup.keys())
    if missing:
        return {
            "ok": False,
            "error": f"followup missing required fields: {sorted(missing)}",
        }
    try:
        new_version = append_to_list(project, slug, "followups", followup, expected_version)
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)


def _resolve_followup(
    project: str,
    slug: str,
    followup_id: str,
    outcome: str,
    by: str,
    expected_version: int,
) -> dict[str, Any]:
    """Mark a followup as resolved.

    Sets resolved_at, resolved_by, outcome on the followup with the given id.
    """
    updates = {
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "resolved_by": by,
        "outcome": outcome,
    }
    try:
        new_version = resolve_in_list(
            project, slug, "followups", followup_id, updates, expected_version
        )
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)
    except KeyError as e:
        return {"ok": False, "error": str(e)}


def _set_status(
    project: str,
    slug: str,
    status: str,
    expected_version: int,
) -> dict[str, Any]:
    """Update data.status.

    Valid values: active | pending | blocked | shipped | draft | archived
    """
    valid = {"active", "pending", "blocked", "shipped", "draft", "archived"}
    if status not in valid:
        return {"ok": False, "error": f"status must be one of {sorted(valid)}"}
    try:
        new_version = patch_plan(project, slug, {"status": status}, expected_version)
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)


def _set_impl(
    project: str,
    slug: str,
    impl: float,
    expected_version: int,
) -> dict[str, Any]:
    """Update data.impl (implementation fraction, 0.0 to 1.0)."""
    if not 0.0 <= impl <= 1.0:
        return {"ok": False, "error": "impl must be between 0.0 and 1.0"}
    try:
        new_version = patch_plan(project, slug, {"impl": impl}, expected_version)
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)


# ── Register tools with SDK (when available) ───────────────────────────────

if mcp is not None:
    # Wrap each plain function with @mcp.tool(). FastMCP infers the JSON
    # schema from the function signature + type annotations.
    read_plan_tool = mcp.tool()(_read_plan)
    list_plans_tool = mcp.tool()(_list_plans)
    patch_plan_tool = mcp.tool()(_patch_plan)
    append_comment_tool = mcp.tool()(_append_comment)
    lock_decision_tool = mcp.tool()(_lock_decision)
    append_followup_tool = mcp.tool()(_append_followup)
    resolve_followup_tool = mcp.tool()(_resolve_followup)
    set_status_tool = mcp.tool()(_set_status)
    set_impl_tool = mcp.tool()(_set_impl)


# ── Entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    if not _HAS_MCP or mcp is None:
        msg = (
            "mcp package not found. Install with:\n"
            "  uv pip install mcp\n"
            "or via the project:\n"
            "  uv pip install -e ~/Code/reckon\n"
        )
        raise SystemExit(msg)
    # stdio transport is the default for Claude Code MCP servers
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
