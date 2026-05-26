"""reckon MCP server — stdio entrypoint.

Registers all reckon.* tools and delegates IO to _store.py.

Version-write contract mirrors POST /plan/<project>/<slug> in
~/Code/reckon/reckon/serve.py. Both rewrite the plan HTML island atomically
using the same `version` optimistic-concurrency field.  The docs-server owns
the browser UI; this MCP server owns the agent IO path.  They coexist safely
because both use atomic .tmp rename.

For "index" and "project" slugs the old JSON-envelope backing (_version field)
is used unchanged — sprints/milestones live there.

Usage (stdio, the Claude Code default):
    reckon mcp
    # or:
    python -m reckon.mcp

SDK note: this file uses the FastMCP pattern from mcp >= 1.0.0:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("reckon")
    @mcp.tool()
    def my_tool(...): ...
    mcp.run()
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── SDK import ─────────────────────────────────────────────────────────────
try:
    from mcp.server.fastmcp import FastMCP
    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False
    FastMCP = None  # type: ignore[assignment,misc]

from reckon._store import (
    VersionConflict,
    _mounts_path,
    _state_root,
    append_to_list,
    list_followups_across,
    list_questions_across,
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
            "doesn't match the plan's current version."
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

def _read_plan(project: str, slug: str) -> dict[str, Any]:
    """Return the full data blob for one plan, plus its current version.

    For plan slugs, data is the raw HTML island (version = island["version"]).
    For "index"/"project" slugs, data is the JSON envelope data sub-object.

    Returns { project, slug, version, data }.
    """
    data, version = read_plan(project, slug)
    return {"project": project, "slug": slug, "version": version, "data": data}


def _list_plans(project: str, status: str | None = None) -> dict[str, Any]:
    """Return a lightweight index of plans for the project.

    Always uses live HTML meta-tag discovery so impl/status are never stale.
    Falls back to index.json inventory only when discovery is unavailable.
    Each entry: { slug, title, status, impl, ms, sprint, roi, effort }.
    If status is given, filters to only plans matching that status value.
    """
    inventory: list[dict] = []
    mounts_path = _mounts_path()
    if mounts_path.exists():
        try:
            mounts = json.loads(mounts_path.read_text())
            docs_dir_str = mounts.get(project)
            if docs_dir_str:
                from reckon.serve import discover_plans
                discovered = discover_plans(Path(docs_dir_str), project, _state_root())
                inventory = discovered.get("inventory", [])
        except Exception:
            pass

    if not inventory:
        # Discovery unavailable — fall back to index.json (may be stale)
        data, _ = read_plan(project, "index")
        inventory = data.get("inventory", [])

    if status:
        inventory = [p for p in inventory if p.get("status") == status]

    return {
        "project": project,
        "plans": [
            {
                "slug":   p.get("slug"),
                "title":  p.get("title"),
                "status": p.get("status"),
                "impl":   p.get("impl"),
                "ms":     p.get("ms"),
                "sprint": p.get("sprint"),
                "roi":    p.get("roi"),
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
    note_id = f"n-{datetime.now(tz=timezone.utc):%Y%m%dT%H%M%S%f}"
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
    """Write data.decisions[key].{choice,rationale,when,by} (merge, not replace).

    The authored fields (title, context, choices[]) are preserved — set_nested
    merges the new lock fields into the existing decision entry rather than
    replacing it wholesale.

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


# ── Sprint management ──────────────────────────────────────────────────────

def _list_sprints(project: str) -> dict[str, Any]:
    """Return sprints[], milestones[], and active_sprint_id from index.json.

    Returns { project, version, active_sprint_id, sprints, milestones }.
    Read this before any sprint write tool to get the current version.
    """
    data, version = read_plan(project, "index")
    return {
        "project":          project,
        "version":          version,
        "active_sprint_id": data.get("active_sprint_id"),
        "sprints":          data.get("sprints", []),
        "milestones":       data.get("milestones", []),
    }


def _update_sprint(
    project: str,
    sprint_id: str,
    updates: dict[str, Any],
    expected_version: int,
) -> dict[str, Any]:
    """Patch fields on a sprint in index.json#sprints[].

    Allowed update keys: status (planned|active|done), theme, description, starts, ends.
    Use add_sprint_item / move_sprint_item to manage items[].
    Setting status "active" auto-updates active_sprint_id; "done" clears it.
    """
    forbidden = {"items", "id"}
    bad = forbidden & set(updates.keys())
    if bad:
        return {"ok": False, "error": f"use dedicated tools for: {sorted(bad)}"}

    valid_statuses = {"planned", "active", "done"}
    if "status" in updates and updates["status"] not in valid_statuses:
        return {"ok": False, "error": f"sprint status must be one of {sorted(valid_statuses)}"}

    cur_data, cur_version = read_plan(project, "index")
    if expected_version != cur_version:
        return _conflict_response(VersionConflict(expected_version, cur_version, cur_data))

    sprints = list(cur_data.get("sprints", []))
    found = False
    warning = None
    for i, s in enumerate(sprints):
        if s.get("id") == sprint_id:
            if updates.get("status") == "active":
                already = next((x for x in sprints if x.get("status") == "active" and x.get("id") != sprint_id), None)
                if already:
                    warning = f"sprint {already['id']} is already active — consider closing it first"
            sprints[i] = {**s, **updates}
            active_id = cur_data.get("active_sprint_id")
            if updates.get("status") == "active":
                cur_data["active_sprint_id"] = sprint_id
            elif updates.get("status") == "done" and active_id == sprint_id:
                cur_data["active_sprint_id"] = None
            found = True
            break

    if not found:
        return {"ok": False, "error": f"sprint {sprint_id!r} not found"}

    try:
        new_version = write_plan(project, "index", {**cur_data, "sprints": sprints}, cur_version)
        result: dict[str, Any] = {"ok": True, "project": project, "sprint_id": sprint_id, "new_version": new_version}
        if warning:
            result["warning"] = warning
        return result
    except VersionConflict as e:
        return _conflict_response(e)


def _add_sprint_item(
    project: str,
    sprint_id: str,
    item: dict[str, Any] | str,
    expected_version: int,
) -> dict[str, Any]:
    """Append an item to sprint.items[] in index.json.

    item is a slug string or an object with:
      { slug (required), why_now, tier, done_when, status }
    Duplicate slugs within the same sprint are rejected.
    """
    cur_data, cur_version = read_plan(project, "index")
    if expected_version != cur_version:
        return _conflict_response(VersionConflict(expected_version, cur_version, cur_data))

    slug = item if isinstance(item, str) else item.get("slug", "")
    if not slug:
        return {"ok": False, "error": "item must have a slug"}

    sprints = list(cur_data.get("sprints", []))
    found = False
    for i, s in enumerate(sprints):
        if s.get("id") == sprint_id:
            items = list(s.get("items", []))
            existing = {(x if isinstance(x, str) else x.get("slug", "")) for x in items}
            if slug in existing:
                return {"ok": False, "error": f"{slug!r} already in sprint {sprint_id}"}
            items.append(item)
            sprints[i] = {**s, "items": items}
            found = True
            break

    if not found:
        return {"ok": False, "error": f"sprint {sprint_id!r} not found"}

    try:
        new_version = write_plan(project, "index", {**cur_data, "sprints": sprints}, cur_version)
        return {"ok": True, "project": project, "sprint_id": sprint_id, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)


def _move_sprint_item(
    project: str,
    slug: str,
    from_sprint: str,
    to_sprint: str,
    expected_version: int,
) -> dict[str, Any]:
    """Move a plan item from one sprint to another in index.json.

    Preserves any item metadata (why_now, tier, done_when, etc.).
    """
    cur_data, cur_version = read_plan(project, "index")
    if expected_version != cur_version:
        return _conflict_response(VersionConflict(expected_version, cur_version, cur_data))

    sprints = list(cur_data.get("sprints", []))
    sprint_map: dict[str, tuple[int, dict]] = {
        s["id"]: (i, s) for i, s in enumerate(sprints) if "id" in s
    }

    if from_sprint not in sprint_map:
        return {"ok": False, "error": f"from_sprint {from_sprint!r} not found"}
    if to_sprint not in sprint_map:
        return {"ok": False, "error": f"to_sprint {to_sprint!r} not found"}

    fi, fs = sprint_map[from_sprint]
    ti, ts = sprint_map[to_sprint]

    from_items = list(fs.get("items", []))
    item_obj = None
    new_from: list = []
    for it in from_items:
        it_slug = it if isinstance(it, str) else it.get("slug", "")
        if it_slug == slug:
            item_obj = it
        else:
            new_from.append(it)

    if item_obj is None:
        return {"ok": False, "error": f"{slug!r} not found in sprint {from_sprint}"}

    to_items = list(ts.get("items", []))
    existing_to = {(x if isinstance(x, str) else x.get("slug", "")) for x in to_items}
    if slug in existing_to:
        return {"ok": False, "error": f"{slug!r} already in sprint {to_sprint}"}

    to_items.append(item_obj)
    sprints[fi] = {**fs, "items": new_from}
    sprints[ti] = {**ts, "items": to_items}

    try:
        new_version = write_plan(project, "index", {**cur_data, "sprints": sprints}, cur_version)
        return {
            "ok": True, "project": project, "slug": slug,
            "from_sprint": from_sprint, "to_sprint": to_sprint,
            "new_version": new_version,
        }
    except VersionConflict as e:
        return _conflict_response(e)


def _update_inventory_item(
    project: str,
    slug: str,
    updates: dict[str, Any],
    expected_version: int,
) -> dict[str, Any]:
    """Update a plan's metadata entry in index.json#inventory[].

    Common fields: status, impl, dec_open, sprint, last, roi, effort, ms.
    Does not create new entries — use reckon sync to register plans.
    """
    cur_data, cur_version = read_plan(project, "index")
    if expected_version != cur_version:
        return _conflict_response(VersionConflict(expected_version, cur_version, cur_data))

    inventory = list(cur_data.get("inventory", []))
    found = False
    for i, p in enumerate(inventory):
        if p.get("slug") == slug:
            inventory[i] = {**p, **updates}
            found = True
            break

    if not found:
        return {"ok": False, "error": f"{slug!r} not found in inventory — run reckon sync to register it"}

    try:
        new_version = write_plan(project, "index", {**cur_data, "inventory": inventory}, cur_version)
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)


# ── Cross-plan read tools ──────────────────────────────────────────────────

def _list_followups(project: str, unresolved_only: bool = True) -> dict[str, Any]:
    """Return all followups across every per-plan state file in a project.

    Each entry includes plan_slug and plan_title alongside the followup fields.
    Use unresolved_only=False to include resolved followups too.
    """
    items = list_followups_across(project, unresolved_only=unresolved_only)
    return {"project": project, "count": len(items), "followups": items}


def _list_questions(project: str, unresolved_only: bool = True) -> dict[str, Any]:
    """Return all questions across every per-plan state file in a project.

    Each entry includes plan_slug and plan_title alongside the question fields.
    """
    items = list_questions_across(project, unresolved_only=unresolved_only)
    return {"project": project, "count": len(items), "questions": items}


def _list_projects() -> dict[str, Any]:
    """Return all projects registered in mounts.json.

    Returns { projects: [{name, docs_path}] }.
    """
    mounts_file = _mounts_path()
    if not mounts_file.exists():
        return {"projects": [], "hint": "no mounts.json found — run reckon sync to register a project"}
    try:
        mounts = json.loads(mounts_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "could not read mounts.json"}
    return {
        "projects": [
            {"name": k, "docs_path": v}
            for k, v in mounts.items()
            if not k.startswith("_")
        ]
    }


# ── Per-plan write tools ───────────────────────────────────────────────────

def _resolve_question(
    project: str,
    slug: str,
    question_id: str,
    resolution: str,
    by: str,
    expected_version: int,
) -> dict[str, Any]:
    """Mark a question in data.questions[] as resolved.

    Sets resolved_at, resolved_by, resolution on the entry with the given id.
    """
    updates = {
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "resolved_by": by,
        "resolution": resolution,
    }
    try:
        new_version = resolve_in_list(project, slug, "questions", question_id, updates, expected_version)
        return {"ok": True, "project": project, "slug": slug, "question_id": question_id, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)
    except KeyError as e:
        return {"ok": False, "error": str(e)}


def _add_research(
    project: str,
    slug: str,
    item: dict[str, Any],
    expected_version: int,
) -> dict[str, Any]:
    """Append a research item to data.research[].

    Recommended fields: id, type, title, source, added_by, when.
    Optional: url, notes.
    """
    recommended = {"id", "type", "title", "source", "added_by", "when"}
    missing = recommended - set(item.keys())
    if missing:
        return {"ok": False, "error": f"research item missing fields: {sorted(missing)}"}
    try:
        new_version = append_to_list(project, slug, "research", item, expected_version)
        return {"ok": True, "project": project, "slug": slug, "new_version": new_version}
    except VersionConflict as e:
        return _conflict_response(e)


# ── Register tools with SDK ────────────────────────────────────────────────

if mcp is not None:
    read_plan_tool            = mcp.tool()(_read_plan)
    list_plans_tool           = mcp.tool()(_list_plans)
    patch_plan_tool           = mcp.tool()(_patch_plan)
    append_comment_tool       = mcp.tool()(_append_comment)
    lock_decision_tool        = mcp.tool()(_lock_decision)
    append_followup_tool      = mcp.tool()(_append_followup)
    resolve_followup_tool     = mcp.tool()(_resolve_followup)
    set_status_tool           = mcp.tool()(_set_status)
    set_impl_tool             = mcp.tool()(_set_impl)
    # Sprint management
    list_sprints_tool         = mcp.tool()(_list_sprints)
    update_sprint_tool        = mcp.tool()(_update_sprint)
    add_sprint_item_tool      = mcp.tool()(_add_sprint_item)
    move_sprint_item_tool     = mcp.tool()(_move_sprint_item)
    update_inventory_item_tool = mcp.tool()(_update_inventory_item)
    # Cross-plan reads
    list_followups_tool       = mcp.tool()(_list_followups)
    list_questions_tool       = mcp.tool()(_list_questions)
    list_projects_tool        = mcp.tool()(_list_projects)
    # Per-plan writes
    resolve_question_tool     = mcp.tool()(_resolve_question)
    add_research_tool         = mcp.tool()(_add_research)


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
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
