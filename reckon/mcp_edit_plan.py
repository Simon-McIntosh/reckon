# ruff: noqa: I001
from __future__ import annotations

import hashlib
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
    parse_plan_ref,
)
from reckon._store import (
    OpError,
    VersionConflict,
    _docs_dir_for_project,
    _find_by_id,
    _mounts_path,
    _receipt_scope_key,
    _resolve_html_file,
    append_to_list,
    apply_ops,
    collect_refused_ops,
    list_followups_across,
    list_questions_across,
    new_plan_html,
    patch_plan,
    read_plan,
    read_write_receipts,
    record_write_receipt,
    replace_plan_text,
    replace_plan_text_batch,
    resolve_in_list,
    set_nested,
    sprint_item_slug,
    write_plan,
)
from reckon._store_section_ops import DECLARATIONS_PATH_HEAD
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
    accepted_op_verbs,
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
    _plan_readiness_fields,
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


#: Sentinel for a path that does not resolve, distinct from a stored ``None``.
_ABSENT = object()


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


def _plan_for_sprint_move(project: str, slug: str, root: str | None) -> tuple[str, Any]:
    """Classify the plan a moved sprint item names.

    Three outcomes, because a sprint's member list holds more than one kind of
    ref and only one of them owes a plan write:

    * ``("foreign", ref)`` — the ref is qualified with another project. That
      plan exists and this call cannot write it, so a move of it is refused
      rather than half-applied.
    * ``("plan", (slug, data, version))`` — a plan of this project that exists,
      with the version its own write is made against.
    * ``("none", None)`` — no plan write is owed: an unparsable ref, or a ref
      naming no plan of this project. A member list may name a plan that is not
      here, and the move still applies to the sprints.
    """
    parsed = parse_plan_ref(slug)
    if parsed is None:
        return "none", None
    if parsed.project and parsed.project != project:
        return "foreign", slug
    data, version = read_plan(project, parsed.slug, root, artifact_type="plan")
    if not data:
        return "none", None
    return "plan", (parsed.slug, data, version)


def _unwritable_move_refusal(
    project: str, plan_slug: str, from_sprint: str, to_sprint: str
) -> dict[str, Any]:
    """Refuse a move whose plan this call cannot write, naming both sprints.

    A sprint item and the plan it names each record the membership. A member
    list may name a plan another project owns, and that plan's ``plan-sprint``
    cannot be written from here, so applying the sprint half of the move alone
    would leave the two ends disagreeing. Refusing names both sprints rather
    than moving the item and leaving the plan behind it.
    """
    return {
        "ok": False,
        "error": "sprint_move_plan_unwritable",
        "project": project,
        "slug": plan_slug,
        "from_sprint": from_sprint,
        "to_sprint": to_sprint,
        "detail": (
            f"plan {plan_slug!r} is not this project's to write, so the move "
            f"from {from_sprint!r} to {to_sprint!r} is refused and nothing was "
            "written; the plan's own sprint is the other half of the "
            "membership this move changes"
        ),
    }


def _sprint_move_plan_writes(
    project: str, root: str | None, ops: list[dict[str, Any]] | None
) -> tuple[list[tuple[str, dict[str, Any], int, str]], dict[str, Any] | None]:
    """The plan writes an index batch's sprint moves owe, or the refusal.

    An index batch is one batch to one document, so the plan half of a sprint
    move cannot ride along with it. Each move is paired here with the plan write
    that follows the index write, and a move naming a plan this call cannot
    write refuses the batch before the index is touched — otherwise the sprint
    list would change and the plan behind it stay where it was.
    """
    writes: list[tuple[str, dict[str, Any], int, str]] = []
    seen: set[str] = set()
    for op in ops or []:
        if op.get("op") != "move" or op.get("target") != "sprint_item":
            continue
        to_sprint = str(op.get("to") or "")
        if not to_sprint:
            continue
        kind, payload = _plan_for_sprint_move(project, str(op.get("slug") or ""), root)
        if kind == "foreign":
            return [], _unwritable_move_refusal(
                project, str(payload), str(op.get("from") or ""), to_sprint
            )
        if kind != "plan":
            continue
        plan_slug, data, version = payload
        if plan_slug in seen:
            continue
        seen.add(plan_slug)
        writes.append((plan_slug, data, version, to_sprint))
    return writes, None


def _carry_plan_sprints(
    project: str,
    root: str | None,
    writes: list[tuple[str, dict[str, Any], int, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Write each moved plan's sprint through its own versioned write.

    Returns ``(written, failed)``. A plan write that fails after its sprint
    moved is reported as failed rather than raised: the sprint move has landed,
    and its caller owes the reader that fact instead of a plain error.
    """
    written: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for plan_slug, data, version, to_sprint in writes:
        try:
            new_version = write_plan(
                project,
                plan_slug,
                {**data, "sprint": to_sprint},
                version,
                root,
                artifact_type="plan",
            )
        except (VersionConflict, OpError, OSError, ValueError) as exc:
            failed.append({"slug": plan_slug, "sprint": to_sprint, "detail": str(exc)})
            continue
        written.append(
            {"slug": plan_slug, "sprint": to_sprint, "new_version": new_version}
        )
    return written, failed


def _partial_plan_write(
    result: dict[str, Any],
    written: list[dict[str, Any]],
    failed: list[dict[str, Any]],
) -> dict[str, Any]:
    """Report the plan half of a landed sprint move, including a failure."""
    result["plan_writes"] = written
    if not failed:
        return result
    result["ok"] = False
    result["error"] = "partial_write"
    result["partial_write"] = failed
    result["detail"] = (
        "the sprint move landed and the plan write did not; the plan(s) named "
        "in partial_write still record the sprint they were moved from"
    )
    return result


def _index_sprint_membership(
    project: str, root: str | None, plan_slug: str
) -> str | None:
    """The sprint the index lists a plan under, or ``None`` for no sprint.

    The index is the authority for which sprint a plan belongs to, so a retried
    partial move asks it rather than the plan it was unable to write.
    """
    data, _ = read_plan(project, "index", root)
    for sprint in data.get("sprints", []) or []:
        if not isinstance(sprint, dict):
            continue
        for item in sprint.get("items", []) or []:
            if sprint_item_slug(item) == plan_slug:
                return str(sprint.get("id") or "")
    return None


def _finish_pending_plan_writes(
    project: str, root: str | None, pending: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Write the plan sprints a partial move still owes, reading the index.

    Returns ``(resolved, still_pending)``. The batch is never re-applied: its
    index move has landed, and re-applying it would be refused. Each pending
    entry is decided against the index's current membership:

    * still listed in the sprint the move sent it to, and its own sprint
      differs — the plan's sprint is written against the plan's current
      version, so an unrelated edit since the failure does not stop it;
    * still listed there, and its own sprint already agrees — settled, as after
      a crash between the plan write and the receipt update;
    * listed in another sprint, in no sprint, or gone — superseded by the write
      that got there first, so nothing is written.

    A plan that still cannot be written stays pending.
    """
    resolved: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    for entry in pending:
        plan_slug = str(entry.get("slug") or "")
        to_sprint = str(entry.get("sprint") or "")
        if not plan_slug or not to_sprint:
            continue
        listed = _index_sprint_membership(project, root, plan_slug)
        if listed != to_sprint:
            resolved.append(
                {
                    "slug": plan_slug,
                    "sprint": to_sprint,
                    "outcome": "superseded",
                    "listed_in": listed,
                }
            )
            continue
        data, version = read_plan(project, plan_slug, root, artifact_type="plan")
        if not data:
            resolved.append(
                {
                    "slug": plan_slug,
                    "sprint": to_sprint,
                    "outcome": "superseded",
                    "listed_in": listed,
                }
            )
            continue
        if data.get("sprint") == to_sprint:
            resolved.append(
                {"slug": plan_slug, "sprint": to_sprint, "outcome": "settled"}
            )
            continue
        try:
            new_version = write_plan(
                project,
                plan_slug,
                {**data, "sprint": to_sprint},
                version,
                root,
                artifact_type="plan",
            )
        except (VersionConflict, OpError, OSError, ValueError) as exc:
            still_pending.append(
                {"slug": plan_slug, "sprint": to_sprint, "detail": str(exc)}
            )
            continue
        resolved.append(
            {
                "slug": plan_slug,
                "sprint": to_sprint,
                "outcome": "written",
                "new_version": new_version,
            }
        )
    return resolved, still_pending


def _resume_partial_plan_writes(
    project: str,
    slug: str,
    doc_type: str | None,
    root: str | None,
    receipt_scope: str,
    request_id: str,
    digest: str,
    recorded: dict[str, Any],
) -> dict[str, Any]:
    """Answer a retry whose receipt still owes plan sprint writes.

    The index batch landed, so the retry finishes those writes rather than
    replaying the batch. A retry that leaves a plan pending reports the partial
    write again; one that leaves none returns success naming each plan written,
    settled or superseded, and records the request as complete so a further
    retry replays as plain success.
    """
    pending = recorded.get("pending") or []
    resolved, still_pending = _finish_pending_plan_writes(project, root, pending)
    landed_version = recorded.get("version")
    base = _edit_success_response(
        project=project, slug=slug, doc_type=doc_type, new_version=landed_version
    )
    base["replayed"] = True
    base["plan_write_outcomes"] = resolved
    if still_pending:
        result = _partial_plan_write(
            base,
            [entry for entry in resolved if entry["outcome"] == "written"],
            still_pending,
        )
        result["message"] = (
            f"A write for request_id already landed at version {landed_version}; "
            "the plan sprint writes it left pending are still pending."
        )
        return result
    record_write_receipt(
        project,
        receipt_scope,
        request_id,
        version=landed_version,
        digest=digest,
        pending=[],
    )
    base["plan_writes"] = resolved
    base["message"] = (
        f"A write for request_id already landed at version {landed_version}; "
        "this retry finished the plan sprint writes it left pending."
    )
    return base


def _move_sprint_item(
    project: str,
    slug: str,
    from_sprint: str,
    to_sprint: str,
    expected_version: int,
) -> dict[str, Any]:
    """Move a plan item from one sprint to another in index.json.

    Preserves any item metadata (why_now, capability, done_when, etc.). The
    move also writes the plan's own sprint, so the sprint that lists a plan and
    the sprint the plan names stay one answer. A plan this project cannot write
    refuses the whole call before the index is touched.
    """
    refusal = _run_scoped_write_guard(project, "index")
    if refusal is not None:
        return refusal
    kind, plan = _plan_for_sprint_move(project, slug, None)
    if kind == "foreign":
        return _unwritable_move_refusal(project, str(plan), from_sprint, to_sprint)
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
        if sprint_item_slug(it) == slug:
            item_obj = it
        else:
            new_from.append(it)

    if item_obj is None:
        return {"ok": False, "error": f"{slug!r} not found in sprint {from_sprint}"}

    to_items = list(ts.get("items", []))
    if slug in {sprint_item_slug(x) for x in to_items}:
        return {"ok": False, "error": f"{slug!r} already in sprint {to_sprint}"}

    to_items.append(item_obj)
    sprints[fi] = {**fs, "items": new_from}
    sprints[ti] = {**ts, "items": to_items}

    try:
        new_version = write_plan(
            project, "index", {**cur_data, "sprints": sprints}, cur_version
        )
    except VersionConflict as e:
        return _conflict_response(e)

    result = {
        "ok": True,
        "project": project,
        "slug": slug,
        "from_sprint": from_sprint,
        "to_sprint": to_sprint,
        "new_version": new_version,
    }
    if kind == "plan":
        written, failed = _carry_plan_sprints(
            project, None, [(plan[0], plan[1], plan[2], to_sprint)]
        )
        return _partial_plan_write(result, written, failed)
    return result


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


def _value_at_path(data: dict[str, Any], path: str) -> Any:
    """The current value a ``set`` op's path addresses, or ``_ABSENT``.

    Resolves the same address forms the store's set handler accepts: a list of
    records keyed by ``id`` (sections, followups, gates), a mapping (decisions,
    a section's declarations), or a top-level scalar. Anything unresolved is
    reported as absent rather than guessed.
    """
    parts = [p for p in str(path).split(".") if p]
    if not parts:
        return _ABSENT
    head = parts[0]
    listed_heads = {"sections", "followups", "gates", "questions", "research"}
    if len(parts) >= 3 and head in listed_heads:
        listed = data.get(head)
        if not isinstance(listed, list):
            return _ABSENT
        hit = _find_by_id(listed, parts[1])
        if hit is None:
            return _ABSENT
        return hit[1].get(parts[2], _ABSENT)
    if len(parts) == 2 and head in {"decisions", "section_declarations"}:
        mapping = data.get(head)
        if not isinstance(mapping, dict):
            return _ABSENT
        return mapping.get(parts[1], _ABSENT)
    current: Any = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _ABSENT
    return current


def _changed_targets(
    data: dict[str, Any], ops: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The targets a caller's ops address, with the value each holds now.

    A version conflict means the document moved since the caller read it, so the
    answer names every target the refused write would touch beside its current
    value: the caller re-reads only what it did not expect to move, instead of
    re-reading the whole plan.
    """
    targets: list[dict[str, Any]] = []
    for index, op in enumerate(ops or []):
        if not isinstance(op, dict) or op.get("op") != "set":
            continue
        path = op.get("path")
        if not isinstance(path, str) or not path:
            continue
        current = _value_at_path(data, path)
        entry: dict[str, Any] = {"index": index, "path": path}
        if current is not _ABSENT:
            entry["current"] = current
        targets.append(entry)
    return targets


def _attach_changed_targets(
    response: dict[str, Any],
    data: dict[str, Any],
    ops: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Add the addressed targets to a version-conflict response, in place."""
    if response.get("error") != "version_conflict":
        return response
    targets = _changed_targets(data, list(ops or []))
    if targets:
        response["changed_targets"] = targets
    return response


def _changes_section_declarations(ops: list[dict[str, Any]] | None) -> bool:
    """Whether a state-mode write moves a section's declaration.

    ``DECLARATIONS_PATH_HEAD.<id>`` is the landing beat a section moves to done
    through, and the bare head alone replaces the whole map; either is the write
    that can leave a plan newly finished. Any other target — a section's typed
    fields, a decision, a status — cannot, so the readiness the roadmap derives
    is not attached to it. The head is the store's own head constant, so a
    rename there moves this predicate with the dispatch it keys on rather than
    detaching the readiness fields in silence.
    """
    head = DECLARATIONS_PATH_HEAD
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        path = op.get("path")
        if isinstance(path, str) and (path == head or path.startswith(f"{head}.")):
            return True
    return False


def _ops_digest(
    mode: str,
    ops: list[dict[str, Any]] | None,
    *,
    old_html: str | None,
    new_html: str | None,
    replacements: list[dict[str, str]] | None,
) -> str:
    """A stable digest of one write's request, so a reused id is detectable."""
    material = json.dumps(
        {
            "mode": mode,
            "ops": ops or [],
            "old_html": old_html,
            "new_html": new_html,
            "replacements": replacements or [],
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _replayed_response(
    project: str, slug: str, doc_type: str | None, version: int
) -> dict[str, Any]:
    """The success a retried request id returns for the write that landed."""
    result = _edit_success_response(
        project=project, slug=slug, doc_type=doc_type, new_version=version
    )
    result["replayed"] = True
    result["message"] = (
        f"A write for request_id already landed at version {version}; "
        "this is its recorded result."
    )
    return result


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
    request_id: str | None = None,
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
    refusal = _refuse_unknown_op_verbs(ops, _document_type(slug, doc_type))
    if refusal is not None:
        return refusal
    # A write made from inside a crew run belongs to that run's own worktree.
    # Without an explicit checkout_path a run-scoped write resolves there, and
    # a write to any other project's plan is refused rather than silently
    # reaching the mounts-registered main checkout.
    if checkout_path is None:
        scoped_root, scoped_refusal = _run_scoped_checkout(project, slug, doc_type)
        if scoped_refusal is not None:
            return scoped_refusal
        checkout_path = scoped_root
    # A request id makes a retry idempotent: the same id with the same request
    # returns the version an earlier attempt reached, so a write whose reply was
    # lost to a deadline does not read as a fresh conflict. The same id with a
    # different request is a caller error and is refused, never answered with
    # the earlier write's version.
    receipt_scope = None
    request_digest = None
    if request_id:
        receipt_scope = _receipt_scope_key(project, slug, doc_type, checkout_path)
        request_digest = _ops_digest(
            mode,
            ops,
            old_html=old_html,
            new_html=new_html,
            replacements=replacements,
        )
        recorded = read_write_receipts(project, receipt_scope).get(request_id)
        if recorded is not None:
            if recorded.get("digest") != request_digest:
                return {
                    "ok": False,
                    "error": "reused_request_id",
                    "detail": (
                        f"request_id {request_id!r} already answered a write with "
                        "different ops; use a fresh request_id for a new write"
                    ),
                    "request_id": request_id,
                    "landed_version": recorded.get("version"),
                }
            if recorded.get("pending"):
                # The index move landed but a plan sprint did not. Replaying the
                # batch would be refused and would tell the caller the plan kept
                # a sprint it does not, so the retry finishes those writes.
                return _resume_partial_plan_writes(
                    project,
                    slug,
                    doc_type,
                    checkout_path,
                    receipt_scope,
                    request_id,
                    request_digest,
                    recorded,
                )
            return _replayed_response(project, slug, doc_type, recorded.get("version"))
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

    is_index = _is_project_slug(slug, doc_type)
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
            if request_id and receipt_scope is not None and request_digest is not None:
                record_write_receipt(
                    project,
                    receipt_scope,
                    request_id,
                    version=new_version,
                    digest=request_digest,
                )
            return result
        except ProjectStateConflict as exc:
            return _attach_changed_targets(
                _conflict_response(
                    VersionConflict(exc.expected, exc.current, exc.current_data),
                    project=project,
                    slug=slug,
                    doc_type=canonical_doc_type,
                    operation="create" if create else "edit",
                ),
                exc.current_data,
                ops,
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
        return _attach_changed_targets(
            _conflict_response(
                VersionConflict(expected_version, cur_version, cur_data),
                project=project,
                slug=slug,
                doc_type=selected_type,
                operation="create" if create else "edit",
            ),
            cur_data,
            ops,
        )

    # ── a sprint move owes the plan write that carries its other half ──
    # An index batch cannot hold a plan write, so each move is paired with the
    # plan write that follows it. A move whose plan this project cannot write
    # refuses the whole batch here, before the index is touched, rather than
    # moving the item and leaving the plan behind it.
    moved_plan_writes: list[tuple[str, dict[str, Any], int, str]] = []
    if is_index:
        moved_plan_writes, move_refusal = _sprint_move_plan_writes(project, root, ops)
        if move_refusal is not None:
            if created_file is not None:
                created_file.unlink(missing_ok=True)
            return move_refusal

    # ── apply ops to a working copy ──
    import copy

    working = copy.deepcopy(cur_data)
    pre_ops = copy.deepcopy(cur_data)
    state_ops, standalone_declaration = _extract_standalone_declaration(ops)
    try:
        warnings = apply_ops(working, state_ops, is_index)
    except OpError as e:
        # A failed create must leave NO trace — drop the just-written stub so the
        # contract clause "on failure → no write" holds and a retry is unblocked.
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        # Report every op of the batch the store would refuse, each with its
        # index, so a caller learns all of them in one answer. The first refused
        # op's index rides on the OpError the store raised.
        response = _op_error_response(e)
        refusals = collect_refused_ops(pre_ops, state_ops, is_index)
        if refusals:
            response["refused_ops"] = refusals
        if getattr(e, "op_index", None) is not None:
            response["refused_op_index"] = e.op_index
        return response

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
        return _attach_changed_targets(
            _conflict_response(
                e,
                project=project,
                slug=slug,
                doc_type=selected_type,
                operation="create" if create else "edit",
            ),
            e.current_data,
            ops,
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
    failed_plan_writes: list[dict[str, Any]] = []
    if moved_plan_writes:
        written, failed_plan_writes = _carry_plan_sprints(
            project, root, moved_plan_writes
        )
        result = _partial_plan_write(result, written, failed_plan_writes)
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
    if request_id and receipt_scope is not None and request_digest is not None:
        record_write_receipt(
            project,
            receipt_scope,
            request_id,
            version=new_version,
            digest=request_digest,
            pending=[
                {"slug": entry["slug"], "sprint": entry["sprint"]}
                for entry in failed_plan_writes
            ],
        )
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


def _accepted_op_verbs(resource_type: str) -> frozenset[str]:
    """The op verbs the store dispatches for one document type.

    Read from :func:`reckon.project_state.accepted_op_verbs`, which composes
    them from the dispatcher itself, so this pre-flight accepts exactly what the
    store would apply — no more, and nothing it would refuse. The published
    grammar ``read_plan`` returns under ``op_vocab`` describes every op a caller
    may write and is not the dispatch: it lists ``push``, which only a sprint
    accepts.
    """

    return accepted_op_verbs(resource_type)


def _is_project_slug(slug: str | None, doc_type: str | None) -> bool:
    """Whether a request names the project's own resource rather than a plan.

    The rule the write path resolves its target by, held in one place: the
    pre-flight asks it which document type a batch is judged against, and the
    write asks it whether the slug names the index, so the two cannot disagree
    about which resource a request is for.
    """

    return slug in ("index", "project") and doc_type is None


def _document_type(slug: str | None, doc_type: str | None) -> str:
    """The document type the request's own arguments name, without a read.

    The index is a project resource reached by its slug, a plan is the untyped
    default, and every other type is named by ``doc_type``.
    """

    if _is_project_slug(slug, doc_type):
        return "project"
    return canonical_type(doc_type)


def _refuse_unknown_op_verbs(ops: Any, resource_type: str) -> dict[str, Any] | None:
    """Refuse an op the named document type does not dispatch, before any read.

    An op verb is an argument: the store answers the same refusal, but only
    after it has read the document to apply the batch to. Judging the verb
    against the dispatch for the request's own document type lets a malformed
    request be refused in milliseconds whatever storage is doing, and refuses
    what the store would refuse — ``push`` is spelled in the published grammar
    and accepted by a sprint alone, so a batch pushing a plan is settled here
    rather than after the plan has been read. A batch that is not a list, or an
    entry that is not an object, is left to the store, which names the position
    it refused.
    """
    if not isinstance(ops, list):
        return None
    accepted = _accepted_op_verbs(resource_type)
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            continue
        verb = op.get("op")
        if verb in accepted:
            continue
        return {
            "ok": False,
            "error": "op_error",
            "detail": f"op #{index}: a {resource_type} does not accept the verb "
            f"{verb!r}; accepted verbs are " + ", ".join(sorted(accepted)),
        }
    return None


def _uncovered_before_write(
    project: str,
    slug: str,
    checkout_path: str | None,
    doc_type: str | None,
    *,
    create: bool,
) -> set[str] | None:
    """The units a plan reported uncovered just before a write lands.

    ``review_owed`` on the answer is the plan's whole debt, so the write's own
    contribution is that set minus this one. A create has no content before it,
    so its before-set is empty by definition and every unit it writes is its
    own. A plan whose file cannot be read answers ``None``, which the answer
    reports as unknown rather than as nothing uncovered.
    """

    if create:
        return set()
    root = checkout_path
    if root is None:
        root, _refusal = _run_scoped_checkout(project, slug, doc_type)
    written = _written_path(project, slug, root, doc_type)
    if written is None:
        return None
    from reckon.crew import plan_review

    try:
        _records, uncovered, _changes = plan_review.review_coverage(
            project, slug, plan=Path(written)
        )
    except Exception:  # noqa: BLE001 — reported as unknown on the answer
        return None
    return uncovered


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
    request_id: str | None = None,
) -> dict[str, Any]:
    """Edit one Reckon resource through a version-safe state or text mode.

    Use ``mode='state'`` with ``ops`` for validated structured changes. Use
    ``mode='text'`` with ``old_html`` and ``new_html`` for one exact authored
    HTML replacement, or with a ``replacements`` list of
    ``{old_html, new_html}`` pairs to apply them in order as one versioned
    write; a refused pair refuses the whole batch, naming the pair's index.
    Read the same resource first and pass its version as ``expected_version``;
    worktree callers must reuse the same ``checkout_path`` on both calls.

    ``request_id`` makes a retry idempotent: the same id with the same request
    returns the version the first attempt reached with ``ok`` and
    ``replayed=true``, so a write whose reply was lost to a deadline does not
    read as a fresh conflict. The same id with different ops is refused as a
    reused id rather than answered with the earlier write's version.

    Creating a plan at or beyond the project's declared pending-plan limit
    still succeeds; the success response carries a ``warning`` naming the limit,
    the pending count and the three pending plans nearest to closing. A create
    within the limit, or in a project declaring no limit, carries no warning.
    """

    # The plan's coverage before the write is read here, because the write is
    # what destroys it: ``review_owed`` on the answer is the plan's whole debt,
    # and only the difference against this set is the write's own doing.
    uncovered_before = (
        _uncovered_before_write(project, slug, checkout_path, doc_type, create=create)
        if _document_type(slug, doc_type) == "plan"
        else None
    )
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
        request_id=request_id,
    )
    # A plan write reports the review it owes; a doc write and an index write
    # do not, so the note is attached only to a plan.
    if result.get("ok") and result.get("resource", {}).get("type") == "plan":
        result.update(
            _review_owed_fields(
                project, slug, result.get("path"), uncovered_before=uncovered_before
            )
        )
        # A write that moves a section declaration can be the one that leaves
        # the plan finished, so that write also answers with the plan's
        # readiness and what still stands before its close.
        if mode == "state" and _changes_section_declarations(ops):
            result.update(_plan_readiness_fields(project, slug, checkout_path))
    return result
