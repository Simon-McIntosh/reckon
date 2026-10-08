# ruff: noqa: I001
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

# ── SDK import ─────────────────────────────────────────────────────────────
from reckon import (
    velocity as velocity_module,
)
from reckon._schema import (
    TYPE_ENUM,
    IndexData,
    PlanState,
)
from reckon._store import (
    OpError,
    VersionConflict,
    _docs_dir_for_project,
    _mounts_path,
    _resolve_html_file,
    append_to_list,
    apply_ops,
    list_followups_across,
    list_questions_across,
    new_plan_html,
    patch_plan,
    read_plan,
    replace_plan_text,
    replace_plan_text_batch,
    resolve_in_list,
    set_nested,
    write_plan,
)
from reckon.capability import (
    from_legacy_tier,
    validate_capability,
)
from reckon.project_state import (
    RESOURCE_TYPES as PROJECT_RESOURCE_TYPES,
)
from reckon.project_state import (
    LegacyIndexReadOnly,
    ProjectStateConflict,
    ProjectStateError,
    apply_resource_ops,
    resource_path,
)
from reckon.resources import (
    ResourceCollision,
    canonical_type,
    content_provenance,
    resolve_resource,
)
from reckon.serve import _resolve_plan_file
from reckon.mcp_deadlines import (
    _conflict_response,
    _edit_success_response,
    _op_error_response,
    _review_owed_fields,
    _run_scoped_checkout,
    _run_scoped_write_guard,
    _written_path,
)
from reckon.mcp_discovery import (
    _apply_standalone_meta,
    _discover_project,
    _extract_standalone_declaration,
    _inventory_row,
    _unwired_plan_refusal,
)


def _patch_plan(
    project: str,
    slug: str,
    patch: dict[str, Any],
    expected_version: int,
) -> dict[str, Any]:
    """Apply a JSON merge-patch to the plan data blob.

    Only top-level keys are merged. For nested fields (decisions, followups),
    use the dedicated tools (reckon.lock_decision, reckon.append_followup, etc.).

    Returns { ok, project, slug, new_version } or a version_conflict error.
    """
    refusal = _run_scoped_write_guard(project, slug)
    if refusal is not None:
        return refusal
    try:
        new_version = patch_plan(project, slug, patch, expected_version)
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
        }
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
    """Append a comment to data.comments[section_id] (the section-anchored map
    the plan page renders as <section data-reckon="comments">).

    comment shape: { id, who, when, body, quote? }
    """
    refusal = _run_scoped_write_guard(project, slug)
    if refusal is not None:
        return refusal
    cur_data, cur_version = read_plan(project, slug)
    if expected_version != cur_version:
        return _conflict_response(
            VersionConflict(expected_version, cur_version, cur_data)
        )

    comments = dict(cur_data.get("comments", {}))
    arr = list(comments.get(section_id, []))
    comment_id = f"c-{datetime.now(tz=timezone.utc):%Y%m%dT%H%M%S%f}"
    comment: dict[str, Any] = {
        "id": comment_id,
        "who": author,
        "when": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "body": body,
    }
    if quote:
        comment["quote"] = quote
    arr.append(comment)
    comments[section_id] = arr

    try:
        new_version = write_plan(
            project, slug, {**cur_data, "comments": comments}, cur_version
        )
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
            "comment_id": comment_id,
        }
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
    refusal = _run_scoped_write_guard(project, slug)
    if refusal is not None:
        return refusal
    decision = {
        "choice": choice,
        "rationale": rationale,
        "when": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "by": by,
    }
    try:
        new_version = set_nested(
            project, slug, "decisions", key, decision, expected_version
        )
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
        }
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
    The prompt field is one ``/reckon-build`` invocation line; the plan owns all
    semantic guidance.
    """
    refusal = _run_scoped_write_guard(project, slug)
    if refusal is not None:
        return refusal
    required = {"id", "written_by", "written_at", "title", "body", "prompt"}
    missing = required - set(followup.keys())
    if missing:
        return {
            "ok": False,
            "error": f"followup missing required fields: {sorted(missing)}",
        }
    try:
        new_version = append_to_list(
            project, slug, "followups", followup, expected_version
        )
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
        }
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
    refusal = _run_scoped_write_guard(project, slug)
    if refusal is not None:
        return refusal
    updates = {
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "resolved_by": by,
        "outcome": outcome,
    }
    try:
        new_version = resolve_in_list(
            project, slug, "followups", followup_id, updates, expected_version
        )
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
        }
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
    refusal = _run_scoped_write_guard(project, slug)
    if refusal is not None:
        return refusal
    try:
        new_version = patch_plan(project, slug, {"status": status}, expected_version)
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
        }
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
    refusal = _run_scoped_write_guard(project, slug)
    if refusal is not None:
        return refusal
    try:
        new_version = patch_plan(project, slug, {"impl": impl}, expected_version)
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
        }
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
        "project": project,
        "version": version,
        "active_sprint_id": data.get("active_sprint_id"),
        "sprints": data.get("sprints", []),
        "milestones": data.get("milestones", []),
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
    refusal = _run_scoped_write_guard(project, "index")
    if refusal is not None:
        return refusal
    forbidden = {"items", "id"}
    bad = forbidden & set(updates.keys())
    if bad:
        return {"ok": False, "error": f"use dedicated tools for: {sorted(bad)}"}

    valid_statuses = {"planned", "open", "active", "done"}
    if "status" in updates and updates["status"] not in valid_statuses:
        return {
            "ok": False,
            "error": f"sprint status must be one of {sorted(valid_statuses)}",
        }

    cur_data, cur_version = read_plan(project, "index")
    if expected_version != cur_version:
        return _conflict_response(
            VersionConflict(expected_version, cur_version, cur_data)
        )

    sprints = list(cur_data.get("sprints", []))
    found = False
    warning = None
    for i, s in enumerate(sprints):
        if s.get("id") == sprint_id:
            if updates.get("status") == "active":
                already = next(
                    (
                        x
                        for x in sprints
                        if x.get("status") == "active" and x.get("id") != sprint_id
                    ),
                    None,
                )
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
        new_version = write_plan(
            project, "index", {**cur_data, "sprints": sprints}, cur_version
        )
        result: dict[str, Any] = {
            "ok": True,
            "project": project,
            "sprint_id": sprint_id,
            "new_version": new_version,
        }
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
      { slug (required), why_now, capability, done_when, status }
    Duplicate slugs within the same sprint are rejected.
    """
    refusal = _run_scoped_write_guard(project, "index")
    if refusal is not None:
        return refusal
    cur_data, cur_version = read_plan(project, "index")
    if expected_version != cur_version:
        return _conflict_response(
            VersionConflict(expected_version, cur_version, cur_data)
        )

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
        new_version = write_plan(
            project, "index", {**cur_data, "sprints": sprints}, cur_version
        )
        return {
            "ok": True,
            "project": project,
            "sprint_id": sprint_id,
            "slug": slug,
            "new_version": new_version,
        }
    except VersionConflict as e:
        return _conflict_response(e)


def _create_sprint(
    project: str,
    sprint_id: str,
    theme: str,
    expected_version: int,
    status: str = "planned",
    starts: str | None = None,
    ends: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a NEW sprint in index.json#sprints[].

    Fills the gap left by update_sprint / add_sprint_item, both of which require
    the sprint to already exist. ``status`` is planned|active|done; "active"
    also sets active_sprint_id (and warns if another sprint was active). Rejects
    a sprint_id that already exists — use update_sprint to edit one in place.

    Returns { ok, project, sprint_id, new_version[, warning] } or a conflict.
    """
    refusal = _run_scoped_write_guard(project, "index")
    if refusal is not None:
        return refusal
    valid_statuses = {"planned", "open", "active", "done"}
    if status not in valid_statuses:
        return {
            "ok": False,
            "error": f"sprint status must be one of {sorted(valid_statuses)}",
        }

    cur_data, cur_version = read_plan(project, "index")
    if expected_version != cur_version:
        return _conflict_response(
            VersionConflict(expected_version, cur_version, cur_data)
        )

    sprints = list(cur_data.get("sprints", []))
    if any(isinstance(s, dict) and s.get("id") == sprint_id for s in sprints):
        return {
            "ok": False,
            "error": f"sprint {sprint_id!r} already exists — use update_sprint to edit it",
        }

    new_sprint: dict[str, Any] = {
        "id": sprint_id,
        "status": status,
        "theme": theme,
        "items": [],
    }
    if starts:
        new_sprint["starts"] = starts
    if ends:
        new_sprint["ends"] = ends
    if description:
        new_sprint["description"] = description
    new_sprint["summary"] = None
    sprints.append(new_sprint)

    new_data = {**cur_data, "sprints": sprints}
    warning = None
    if status == "active":
        prev = cur_data.get("active_sprint_id")
        if prev and prev != sprint_id:
            warning = f"sprint {prev} was active — consider closing it"
        new_data["active_sprint_id"] = sprint_id

    try:
        new_version = write_plan(project, "index", new_data, cur_version)
        result: dict[str, Any] = {
            "ok": True,
            "project": project,
            "sprint_id": sprint_id,
            "new_version": new_version,
        }
        if warning:
            result["warning"] = warning
        return result
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

    Preserves any item metadata (why_now, capability, done_when, etc.).
    """
    refusal = _run_scoped_write_guard(project, "index")
    if refusal is not None:
        return refusal
    cur_data, cur_version = read_plan(project, "index")
    if expected_version != cur_version:
        return _conflict_response(
            VersionConflict(expected_version, cur_version, cur_data)
        )

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
        new_version = write_plan(
            project, "index", {**cur_data, "sprints": sprints}, cur_version
        )
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "from_sprint": from_sprint,
            "to_sprint": to_sprint,
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
        return _conflict_response(
            VersionConflict(expected_version, cur_version, cur_data)
        )

    inventory = list(cur_data.get("inventory", []))
    found = False
    for i, p in enumerate(inventory):
        if p.get("slug") == slug:
            inventory[i] = {**p, **updates}
            found = True
            break

    if not found:
        return {
            "ok": False,
            "error": f"{slug!r} not found in inventory — run reckon sync to register it",
        }

    try:
        new_version = write_plan(
            project, "index", {**cur_data, "inventory": inventory}, cur_version
        )
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
        }
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
        return {
            "projects": [],
            "hint": "no mounts.json found — run reckon sync to register a project",
        }
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
        new_version = resolve_in_list(
            project, slug, "questions", question_id, updates, expected_version
        )
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "question_id": question_id,
            "new_version": new_version,
        }
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
        return {
            "ok": False,
            "error": f"research item missing fields: {sorted(missing)}",
        }
    try:
        new_version = append_to_list(project, slug, "research", item, expected_version)
        return {
            "ok": True,
            "project": project,
            "slug": slug,
            "new_version": new_version,
        }
    except VersionConflict as e:
        return _conflict_response(e)


# ── edit_plan — the one collapsed write tool ────────────────────────────────


def _validate_working(slug: str, working: dict) -> list[str] | None:
    """Schema-validate the working dict. Returns a list of error lines on
    failure, or None when valid. Constructs the model FROM the dict (never
    mutates a model and dumps it — see reckon/_schema.py header)."""
    try:
        if slug in ("index", "project"):
            IndexData.model_validate(working)
            for sprint_record in working.get("sprints", []):
                if not isinstance(sprint_record, dict):
                    continue
                for item in sprint_record.get("items", []):
                    if not isinstance(item, dict):
                        continue
                    if not item.get("capability") and item.get("tier"):
                        mapped, _ = from_legacy_tier(item["tier"])
                        if mapped:
                            item["capability"] = mapped
                    errors = validate_capability(item.get("capability"))
                    if errors:
                        raise ValueError("\n".join(errors))
                    if item.get("capability"):
                        item.pop("tier", None)
        else:
            state = PlanState.model_validate(working).validate_for_write()
            # Persist the validated canonical shape. This is what turns the
            # legacy ``doc`` alias into ``research`` and removes neutral
            # plan-only defaults from research/evidence writes.
            canonical = state.canonical_dump()
            canonical.pop("compatibility_warnings", None)
            if canonical.get("capability"):
                canonical.pop("tier", None)
            for followup in canonical.get("followups", []):
                if isinstance(followup, dict) and followup.get("capability"):
                    followup.pop("tier", None)
            working.clear()
            working.update(canonical)
    except ValueError as e:
        # Split the multi-line validate_for_write message into discrete lines;
        # pydantic ValidationError stringifies to a useful block too.
        msg = str(e)
        lines = [ln.strip(" -") for ln in msg.splitlines() if ln.strip()]
        return lines or [msg]
    return None


def _project_manifest(project: str, root: str | None) -> dict[str, Any]:
    """The project's configuration row, read the way roadmap reads it.

    ``schedule_horizon_sprints`` and any sibling project-wide ceiling live here.
    """
    data, _version = read_plan(project, "index", root)
    if not isinstance(data, dict):
        return {}
    rows = data.get("projects") or []
    if rows and isinstance(rows[0], dict):
        return rows[0]
    return {}


def _pending_plan_limit(project_manifest: dict[str, Any] | None) -> int | None:
    """Read the declared pending-plan limit, or None when the project declares none.

    A project that declares no positive integer limit is not capped.
    """
    value = (project_manifest or {}).get("pending_plan_limit")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _pending_plans(project: str, root: str | None) -> list[dict[str, Any]]:
    """The project's pending plans, by the velocity plan census's own definition.

    *Pending* is the complement of closed, and closed is imported rather than
    re-derived: a plan is closed when its status is one of the velocity view's
    ``CLOSED_STATUSES`` or its archive flag is set.
    """
    discovered = _discover_project(project, root)
    pending: list[dict[str, Any]] = []
    for item in discovered.get("inventory", []):
        row = _inventory_row(item)
        if row.get("type", "plan") != "plan":
            continue
        if str(row.get("archived") or "") == "1":
            continue
        if str(row.get("status") or "") in velocity_module.CLOSED_STATUSES:
            continue
        pending.append(row)
    return pending


def _plans_nearest_to_closing(
    pending: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    """The ``count`` pending plans closest to done, ranked by impl, highest first.

    impl is the completion fraction a plan authors for itself; a missing or
    unparseable impl ranks last so a plan never sorts above one that declares
    progress.
    """

    def rank(row: dict[str, Any]) -> tuple[float, str]:
        try:
            impl = float(row.get("impl"))
        except (TypeError, ValueError):
            impl = 0.0
        return (-impl, str(row.get("slug") or ""))

    ranked = sorted(pending, key=rank)[:count]
    return [
        {
            "slug": str(row.get("slug") or ""),
            "title": str(row.get("title") or ""),
            "impl": row.get("impl"),
        }
        for row in ranked
    ]


def _pending_limit_warning(
    project: str,
    limit: int,
    pending_count: int,
    nearest: list[dict[str, Any]],
) -> str:
    """One warning sentence naming the limit, the count, and the nearest plans."""

    def describe(row: dict[str, Any]) -> str:
        impl = row.get("impl")
        return f"{row['slug']} (impl {impl})" if impl is not None else f"{row['slug']}"

    named = "; ".join(describe(row) for row in nearest) or "none"
    return (
        f"plan created: project {project!r} already holds "
        f"{pending_count} pending plans, at its limit of {limit}. Nearest to "
        f"closing, by impl (highest first): {named}."
    )


def _edit_plan(
    project: str,
    slug: str,
    ops: list[dict[str, Any]] | None,
    expected_version: int,
    create: bool = False,
    checkout_path: str | None = None,
    doc_type: str | None = None,
    mode: Literal["state", "text"] = "state",
    old_html: str | None = None,
    new_html: str | None = None,
    replacements: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Edit structured state or authored prose with version protection.

    ``mode='state'`` applies ``ops`` IN ORDER to a working copy, validates the
    resulting schema, and writes atomically. ``mode='text'`` replaces the one
    exact ``old_html`` occurrence with ``new_html`` — or applies a
    ``replacements`` list of ``{old_html, new_html}`` pairs in order as ONE
    versioned write — and refuses any structured state change. A refused pair
    refuses the whole batch, naming the pair's index, with the file and version
    unchanged. Text mode accepts ``ops=None`` or an empty list and does not
    support ``create``. Both modes reject stale ``expected_version`` values.

    Routing: slug="index" → project config (sprints/milestones/timeline/blockers,
    version = data._version); any other slug → typed HTML selected by
    ``doc_type`` (version = state.version). Untyped edits retain compatibility
    only when the leaf slug identifies one live artifact unambiguously.

    Verbs (the "op" key): set | append | resolve | lock | accept | gate | pass |
    fail | retire_prose | move. See read_plan(
    ..., with_schema=True)["op_vocab"] for the full op grammar.

    Create: edit_plan(..., expected_version=0, create=True) on a NON-existent
    plan slug writes a minimal schema-valid template, then applies ops.

    Multi-worktree (``checkout_path``): when an agent runs inside a git worktree
    (a separate checkout of the same repo), pass ``checkout_path`` = the absolute
    path to that checkout's repo root (the directory containing ``docs/``).  The
    write then lands in ``<checkout_path>/docs`` (plan HTML) or
    ``<checkout_path>/docs/state/<project>/`` (index/project config) — i.e. in
    the AGENT'S OWN worktree, so the agent can commit it from there.  Omit it
    (the default) to target the mounts-registered MAIN checkout (existing
    behaviour).  This closes the "MCP write lands in main, agent commits in
    worktree → duplicate" failure mode.  Always pair it with a read_plan that
    used the SAME ``checkout_path`` so ``expected_version`` matches that file.

    Success includes a human ``message`` plus the typed affected ``resource``
    and machine fields ``new_version`` / ``path``. ``path`` is the ABSOLUTE
    file the write landed in, so a caller can reconcile deterministically
    (e.g. ``git -C <dir> status``). Version conflicts include the requested
    operation, resource title/identity, expected/current versions, and the
    smallest corrective action.
    """
    if mode not in {"state", "text"}:
        return {
            "ok": False,
            "error": "invalid_edit_mode",
            "detail": "mode must be 'state' or 'text'",
        }
    # A write made from inside a crew run belongs to that run's own worktree.
    # Without an explicit checkout_path a run-scoped write resolves there, and
    # a write to any other project's plan is refused rather than silently
    # reaching the mounts-registered main checkout.
    if checkout_path is None:
        scoped_root, scoped_refusal = _run_scoped_checkout(project, slug, doc_type)
        if scoped_refusal is not None:
            return scoped_refusal
        checkout_path = scoped_root
    if mode == "text":
        if create:
            return {
                "ok": False,
                "error": "invalid_edit_request",
                "detail": "text mode does not support create=True",
            }
        if ops:
            return {
                "ok": False,
                "error": "invalid_edit_request",
                "detail": "text mode does not accept structured ops",
            }
        if replacements is not None:
            if old_html is None and new_html is None:
                if not isinstance(replacements, list):
                    return {
                        "ok": False,
                        "error": "invalid_edit_request",
                        "detail": (
                            "text mode replacements must be a list of "
                            "{old_html, new_html} pairs; got "
                            f"{type(replacements).__name__}"
                        ),
                    }
                return _edit_plan_prose(
                    project,
                    slug,
                    None,
                    None,
                    expected_version,
                    checkout_path,
                    doc_type,
                    replacements=replacements,
                )
            return {
                "ok": False,
                "error": "invalid_edit_request",
                "detail": (
                    "text mode accepts either old_html/new_html or replacements, "
                    "not both"
                ),
            }
        if old_html is None or not old_html:
            return {
                "ok": False,
                "error": "invalid_edit_request",
                "detail": "text mode requires non-empty old_html",
            }
        if new_html is None:
            return {
                "ok": False,
                "error": "invalid_edit_request",
                "detail": "text mode requires new_html",
            }
        return _edit_plan_prose(
            project,
            slug,
            old_html,
            new_html,
            expected_version,
            checkout_path,
            doc_type,
        )
    if old_html is not None or new_html is not None or replacements is not None:
        return {
            "ok": False,
            "error": "invalid_edit_request",
            "detail": "state mode does not accept old_html, new_html or replacements",
        }
    if ops is None:
        return {
            "ok": False,
            "error": "invalid_edit_request",
            "detail": "state mode requires an ops list",
        }

    is_index = slug in ("index", "project") and doc_type is None
    root = checkout_path  # alias: the tool-surface name vs the store-layer name
    canonical_doc_type = canonical_type(doc_type) if doc_type else None
    if canonical_doc_type in PROJECT_RESOURCE_TYPES:
        docs_dir = _docs_dir_for_project(project, root)
        if docs_dir is None:
            return {
                "ok": False,
                "error": f"no docs dir for project {project!r}",
            }
        try:
            new_version, warnings = apply_resource_ops(
                docs_dir,
                project,
                canonical_doc_type,
                slug,
                ops or [],
                expected_version,
                create=create,
            )
            result = _edit_success_response(
                project=project,
                slug=slug,
                doc_type=canonical_doc_type,
                new_version=new_version,
                created=create,
            )
            result["doc_type"] = canonical_doc_type
            result["path"] = str(
                resource_path(docs_dir, project, canonical_doc_type, slug)
            )
            result["provenance"] = content_provenance(
                docs_dir.parent, Path(result["path"])
            )
            if warnings:
                result["warnings"] = warnings
            if create:
                result["created"] = True
            return result
        except ProjectStateConflict as exc:
            return _conflict_response(
                VersionConflict(exc.expected, exc.current, exc.current_data),
                project=project,
                slug=slug,
                doc_type=canonical_doc_type,
                operation="create" if create else "edit",
            )
        except ProjectStateError as exc:
            return {
                "ok": False,
                "error": "project_state_error",
                "project": project,
                "slug": slug,
                "doc_type": canonical_doc_type,
                "detail": str(exc),
            }
        except (TypeError, ValueError, FileNotFoundError) as exc:
            return {
                "ok": False,
                "error": "resource_edit_error",
                "detail": str(exc),
            }
    if is_index and canonical_doc_type is not None:
        return {"ok": False, "error": "doc_type is not valid for index/project"}
    if create and canonical_doc_type not in {None, "plan"}:
        return {
            "ok": False,
            "error": "typed creation is not supported; create=True creates plans only",
        }

    docs_dir = _docs_dir_for_project(project, root)
    selected_type = canonical_doc_type
    if not is_index and not create and docs_dir is not None:
        slug_matches = []
        candidate_types = [selected_type] if selected_type is not None else TYPE_ENUM
        for candidate_type in candidate_types:
            try:
                resource = resolve_resource(
                    docs_dir,
                    project,
                    slug,
                    candidate_type,
                    include_archived=False,
                )
            except ResourceCollision as exc:
                detail = str(exc)
                if selected_type is None:
                    detail += "; supply doc_type matching the preceding read_plan call"
                return {
                    "ok": False,
                    "error": "ambiguous_resource",
                    "detail": detail,
                }
            if resource is not None:
                slug_matches.append(resource)
        if selected_type is None and len(slug_matches) > 1:
            kinds = ", ".join(sorted(resource.type for resource in slug_matches))
            return {
                "ok": False,
                "error": "ambiguous_resource",
                "detail": (
                    f"resource slug {slug!r} exists as {kinds}; "
                    "supply doc_type matching the preceding read_plan call"
                ),
            }
        if selected_type is None and len(slug_matches) == 1:
            selected_type = slug_matches[0].type

    # ── create path (plan slugs only) ──
    limit: int | None = None
    pending_count = 0
    pending_limit_warning: str | None = None
    if create:
        if is_index:
            return {"ok": False, "error": "cannot create the index slug"}
        if docs_dir is None:
            hint = (
                f"check checkout_path {checkout_path!r} contains a docs/ dir"
                if root is not None
                else "check mounts.json"
            )
            return {
                "ok": False,
                "error": f"no docs dir for project {project!r} — {hint}",
            }
        html_file = docs_dir / "plans" / f"{slug}.html"
        # Reject if a plan already exists at this slug (direct or via resolution).
        if (
            html_file.exists()
            or _resolve_plan_file(docs_dir, slug, "plan", project=project) is not None
        ):
            return {
                "ok": False,
                "error": f"plan {slug!r} already exists — drop create=True to edit it",
            }
        if expected_version != 0:
            return {"ok": False, "error": "create requires expected_version=0"}
        # ── pending-plan limit (warn, never refuse) ──
        # A project whose configuration declares no limit is never warned.
        # Pending uses the velocity plan census's own closed definition, so a
        # plan closed there clears the warning.
        limit = _pending_plan_limit(_project_manifest(project, root))
        if limit is not None:
            pending = _pending_plans(project, root)
            pending_count = len(pending)
            if pending_count >= limit:
                pending_limit_warning = _pending_limit_warning(
                    project, limit, pending_count, _plans_nearest_to_closing(pending, 3)
                )
        html_file.parent.mkdir(parents=True, exist_ok=True)
        html_file.write_text(new_plan_html(project, slug), encoding="utf-8")
        created_file = html_file  # cleaned up below if the create then fails
    else:
        created_file = None

    # ── read current state (after any template write) ──
    cur_data, cur_version = read_plan(project, slug, root, artifact_type=selected_type)
    if not create and not cur_data and not is_index:
        # An empty plan dict for a non-index slug means the HTML file is absent.
        docs_dir = _docs_dir_for_project(project, root)
        if (
            docs_dir is None
            or _resolve_plan_file(
                docs_dir, slug, selected_type or "plan", project=project
            )
            is None
        ):
            return {
                "ok": False,
                "error": f"plan {slug!r} not found — pass create=True to create it",
            }
    if expected_version != cur_version:
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        return _conflict_response(
            VersionConflict(expected_version, cur_version, cur_data),
            project=project,
            slug=slug,
            doc_type=selected_type,
            operation="create" if create else "edit",
        )

    # ── apply ops to a working copy ──
    import copy

    working = copy.deepcopy(cur_data)
    state_ops, standalone_declaration = _extract_standalone_declaration(ops)
    try:
        warnings = apply_ops(working, state_ops, is_index)
    except OpError as e:
        # A failed create must leave NO trace — drop the just-written stub so the
        # contract clause "on failure → no write" holds and a retry is unblocked.
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        return _op_error_response(e)

    retire_preimages = [
        str(op["preimage"]) for op in ops if op.get("op") == "retire_prose"
    ]

    # ── schema-validate the working dict (reject on failure, write nothing) ──
    errors = _validate_working(slug, working)
    if not errors and selected_type is not None and not is_index:
        working_type = canonical_type(working.get("type"))
        if working_type != selected_type:
            errors = [
                f"type: {working_type!r} does not match selected doc_type "
                f"{selected_type!r}"
            ]
    if errors:
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        return {"ok": False, "error": "schema_validation", "details": errors}

    # ── standalone declaration (authored markup, written to the header) ──
    if (
        standalone_declaration is not None
        and canonical_type(working.get("type")) == "plan"
    ):
        working["standalone"] = standalone_declaration or None
        header = _resolve_html_file(project, slug, root, selected_type or "plan")
        if header is not None and header.exists():
            header.write_text(
                _apply_standalone_meta(
                    header.read_text(encoding="utf-8", errors="replace"),
                    standalone_declaration,
                ),
                encoding="utf-8",
            )

    # ── positive control: a plan is born wired or declared standalone ──
    if create and canonical_type(working.get("type")) == "plan":
        refusal = _unwired_plan_refusal(slug, working)
        if refusal:
            if created_file is not None:
                created_file.unlink(missing_ok=True)
            return {"ok": False, "error": "unwired_plan", "detail": refusal}

    # ── persist the working DICT via the version-checked atomic write ──
    try:
        new_version = write_plan(
            project,
            slug,
            working,
            cur_version,
            root,
            artifact_type=selected_type,
            retire_preimages=retire_preimages,
        )
    except VersionConflict as e:
        return _conflict_response(
            e,
            project=project,
            slug=slug,
            doc_type=selected_type,
            operation="create" if create else "edit",
        )
    except LegacyIndexReadOnly as e:
        return {
            "ok": False,
            "error": "legacy_index_read_only",
            "detail": str(e),
            "hint": (
                "Read the composed index for resource_versions, then edit one "
                "named resource with doc_type."
            ),
        }
    except OpError as e:
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        return _op_error_response(e)
    except (ValueError, FileNotFoundError) as e:
        return {"ok": False, "error": "resource_selection", "detail": str(e)}

    result = _edit_success_response(
        project=project,
        slug=slug,
        doc_type=selected_type,
        new_version=new_version,
        data=working,
        created=create,
    )
    result["path"] = _written_path(project, slug, root, selected_type)
    if result["path"] is not None:
        written_docs_dir = _docs_dir_for_project(project, root)
        if written_docs_dir is not None:
            result["provenance"] = content_provenance(
                written_docs_dir.parent, Path(result["path"])
            )
    if warnings:
        result["warnings"] = warnings
    if create:
        result["created"] = True
        if pending_limit_warning is not None:
            result["warning"] = pending_limit_warning
    return result


def _edit_plan_prose(
    project: str,
    slug: str,
    old_html: str | None,
    new_html: str | None,
    expected_version: int,
    checkout_path: str | None = None,
    doc_type: str | None = None,
    replacements: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Replace authored HTML with version protection, one pair or a batch.

    Use this for plan prose, tables, figures, and section bodies.  Each old
    fragment must occur exactly once.  The operation refuses any change to
    plan metadata or ``data-reckon`` state; use ``edit_plan`` for structured
    fields.  Pair the version and ``checkout_path`` with the preceding raw
    ``read_plan`` call.  A ``replacements`` list applies in order as one
    write; without one, the single ``old_html``/``new_html`` pair is done.
    """

    try:
        if replacements is None:
            new_version, path = replace_plan_text(
                project,
                slug,
                old_html,
                new_html,
                expected_version,
                checkout_path,
                doc_type,
            )
        else:
            new_version, path = replace_plan_text_batch(
                project,
                slug,
                replacements,
                expected_version,
                checkout_path,
                doc_type,
            )
        result = _edit_success_response(
            project=project,
            slug=slug,
            doc_type=doc_type,
            new_version=new_version,
        )
        result["operation"] = "edit_text"
        result["path"] = str(path)
        docs_dir = _docs_dir_for_project(project, checkout_path)
        if docs_dir is not None:
            result["provenance"] = content_provenance(docs_dir.parent, path)
        return result
    except VersionConflict as exc:
        return _conflict_response(
            exc,
            project=project,
            slug=slug,
            doc_type=doc_type,
            operation="edit text in",
        )
    except (FileNotFoundError, ValueError) as exc:
        return {
            "ok": False,
            "error": "text_edit_error",
            "project": project,
            "slug": slug,
            "detail": str(exc),
        }


def _edit_plan_tool(
    project: str,
    slug: str,
    expected_version: int,
    mode: Literal["state", "text"] = "state",
    ops: list[dict[str, Any]] | None = None,
    old_html: str | None = None,
    new_html: str | None = None,
    replacements: list[dict[str, str]] | None = None,
    create: bool = False,
    checkout_path: str | None = None,
    doc_type: str | None = None,
) -> dict[str, Any]:
    """Edit one Reckon resource through a version-safe state or text mode.

    Use ``mode='state'`` with ``ops`` for validated structured changes. Use
    ``mode='text'`` with ``old_html`` and ``new_html`` for one exact authored
    HTML replacement, or with a ``replacements`` list of
    ``{old_html, new_html}`` pairs to apply them in order as one versioned
    write; a refused pair refuses the whole batch, naming the pair's index.
    Read the same resource first and pass its version as ``expected_version``;
    worktree callers must reuse the same ``checkout_path`` on both calls.

    Creating a plan at or beyond the project's declared pending-plan limit
    still succeeds; the success response carries a ``warning`` naming the limit,
    the pending count and the three pending plans nearest to closing. A create
    within the limit, or in a project declaring no limit, carries no warning.
    """

    result = _edit_plan(
        project=project,
        slug=slug,
        ops=ops,
        expected_version=expected_version,
        create=create,
        checkout_path=checkout_path,
        doc_type=doc_type,
        mode=mode,
        old_html=old_html,
        new_html=new_html,
        replacements=replacements,
    )
    # A plan write reports the review it owes; a doc write and an index write
    # do not, so the note is attached only to a plan.
    if result.get("ok") and result.get("resource", {}).get("type") == "plan":
        result.update(_review_owed_fields(project, slug, result.get("path")))
    return result
