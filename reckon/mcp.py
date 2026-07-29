"""reckon MCP server — stdio entrypoint.

Registers all reckon.* tools and delegates IO to _store.py.

Version-write contract mirrors POST /plan/<project>/<slug> in
~/Code/reckon/reckon/serve.py. Both rewrite the plan semantic HTML state atomically
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
from collections import Counter
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
    OpError,
    VersionConflict,
    _docs_dir_for_project,
    _mounts_path,
    _state_root,
    append_to_list,
    apply_ops,
    list_followups_across,
    list_questions_across,
    new_plan_html,
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


def _read_plan(
    project: str | None = None,
    slug: str | None = None,
    with_schema: bool = False,
    checkout_path: str | None = None,
    status: str | None = None,
    doc_type: str | None = None,
    sprint: str | None = None,
    milestone: str | None = None,
    owner: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    include_followups: bool = True,
    include_questions: bool = True,
) -> dict[str, Any]:
    """Read plan state — the single read entrypoint (folds the read tools in).

    Three modes (all additive — the original (project, slug) shape is unchanged):

      read_plan(project, slug)
          → { project, slug, version, data } — one plan's parsed state, or the
            index/project JSON envelope data for those special slugs.

      read_plan(project, slug, with_schema=True)
          → the above PLUS "schema" (the published JSON Schema), a compact
            dos/don'ts note, and an op-vocabulary summary — the context injector
            an agent reads before calling edit_plan.

      read_plan(project)                 [slug omitted/None]
          → DISCOVERY: { project, plans, followups, questions, sprints,
            milestones, active_sprint_id, summary } — folds list_plans /
            list_followups / list_questions / list_sprints into one call.
            Optional filters: status, doc_type, sprint, milestone, owner,
            search, limit. ``include_followups`` / ``include_questions`` trim
            payload size without losing the plan inventory.

      read_plan()  or  read_plan("*")    [project omitted/"*"]
          → { projects: [...] } — folds list_projects.

    Multi-worktree (``checkout_path``):
      When an agent runs inside a git worktree (a separate checkout of the same
      repo whose ``docs/`` tree differs from the registered MAIN checkout), pass
      ``checkout_path`` = the absolute path to that checkout's repo root (the
      directory containing ``docs/``). Reads then use ``<checkout_path>/docs``
      for plan HTML and ``<checkout_path>/docs/state/<project>/`` for
      index/project config — including discovery mode and audit-adjacent rollups.
      Omit it (the default) to read the mounts-registered MAIN checkout.
    """
    # ── projects-list mode ──
    if project is None or project == "*":
        return _list_projects()

    # ── discovery mode (no slug) ──
    if slug is None:
        discovered = _discover_project(project, checkout_path)
        plans = _filter_inventory(
            [_inventory_row(item) for item in discovered.get("inventory", [])],
            status=status,
            doc_type=doc_type,
            sprint=sprint,
            milestone=milestone,
            owner=owner,
            search=search,
            limit=limit,
        )
        selected_slugs = {plan.get("slug") for plan in plans if plan.get("slug")}
        followups_all = list_followups_across(
            project, unresolved_only=True, root=checkout_path
        )
        questions_all = list_questions_across(
            project, unresolved_only=True, root=checkout_path
        )
        followups = [f for f in followups_all if f.get("plan_slug") in selected_slugs]
        questions = [q for q in questions_all if q.get("plan_slug") in selected_slugs]
        index_data, _ = read_plan(project, "index", checkout_path)
        active_sprint_id = index_data.get("active_sprint_id")
        if not active_sprint_id:
            active = next(
                (
                    item.get("id")
                    for item in discovered.get("sprints", [])
                    if isinstance(item, dict) and item.get("status") == "active"
                ),
                None,
            )
            active_sprint_id = active
        return {
            "project": project,
            "plans": plans,
            "followups": followups if include_followups else [],
            "questions": questions if include_questions else [],
            "sprints": discovered.get("sprints", []),
            "milestones": discovered.get("milestones", []),
            "active_sprint_id": active_sprint_id,
            "summary": _discovery_summary(plans, followups, questions),
        }

    # ── single-plan mode (original shape) ──
    data, version = read_plan(project, slug, checkout_path)
    result: dict[str, Any] = {
        "project": project,
        "slug": slug,
        "version": version,
        "data": data,
    }
    deps = data.get("depends_on") if isinstance(data, dict) else None
    if deps:
        result["deps"] = [
            _resolve_plan_ref(ref, project, checkout_path) for ref in deps
        ]
    if with_schema:
        from reckon._schema import gen_json_schema

        result["schema"] = gen_json_schema()
        result["dos_donts"] = _DOS_DONTS
        result["op_vocab"] = _OP_VOCAB
    return result


def _resolve_plan_ref(
    ref: str, owning_project: str, checkout_path: str | None = None
) -> dict[str, Any]:
    """Resolve one link-list ref (``[project:]slug[#stage]``) to live status.

    LOCAL refs resolve inside the owning project, honouring ``checkout_path``.
    EXTERNAL refs always resolve through mounts.json — a worktree of one repo
    has no counterpart checkout of another project, so the registered MAIN
    checkout is the only sensible target. A ref that does not resolve keeps
    ``found: False`` (the audit reports it; the reader decides severity).
    """
    from reckon._schema import parse_plan_ref

    parsed = parse_plan_ref(ref)
    if parsed is None:
        return {"ref": ref, "scope": "invalid", "found": False}
    external = parsed.is_external(owning_project)
    target_project = parsed.project if external else owning_project
    row: dict[str, Any] = {
        "ref": ref,
        "scope": "external" if external else "local",
        "project": target_project,
        "slug": parsed.slug,
        "found": False,
    }
    if parsed.stage:
        row["stage"] = parsed.stage
    try:
        data, _dep_version = read_plan(
            target_project, parsed.slug, None if external else checkout_path
        )
    except Exception:  # noqa: BLE001 — resolution must degrade, not raise
        return row
    if not data:
        return row
    row["found"] = True
    row["status"] = data.get("status", "")
    row["impl"] = data.get("impl", 0)
    row["title"] = data.get("title", "")
    return row


#: Compact dos/don'ts surfaced by read_plan(..., with_schema=True).
_DOS_DONTS = {
    "do": [
        "read_plan first to get the current version; pass it as expected_version.",
        "use edit_plan with an ops list — one call may carry several ops applied in order.",
        "give every followup a non-empty §05 prompt (mandatory; empty is rejected).",
        "slug='index' targets project config (sprints/milestones/timeline/blockers).",
        "use canonical artifact types plan, research, or evidence; doc reads as research.",
        "use project:slug or project:slug#stage provenance refs; unqualified same-project refs remain valid.",
        "depends_on/blocks take the same grammar: bare slug = local, project:slug = external; read_plan(project, slug) resolves them in its deps list.",
    ],
    "dont": [
        "never set plan-version yourself — the server owns it.",
        "off-enum status/roi/effort/tier/type are rejected at the write boundary.",
        "research/evidence cannot carry meaningful plan-only workflow or scheduling fields.",
        "index inventory[] is synthesised live; a set on it is a durable no-op.",
        "create=True on an existing plan, or a normal edit on a missing plan, is rejected.",
    ],
}

#: The edit_plan op vocabulary, inlined for the context injector.
_OP_VOCAB = {
    "set": "{op:'set', path:'<dotted>', value:<any>} — artifact scalars (including informs/evidence_for/verifies and provenance metadata) + decisions.<key>.<field>; index active_sprint_id, sprints.<id>.<field>, milestones.<id>.<field>. impl clamps to 0..1 and is plan-only; sprint status active/done updates active_sprint_id.",
    "append": "{op:'append', target:'<collection>', item:<obj|str>[, section][, key]} — plan followups/research/questions/comments/decisions; index sprints, sprints.<id>.items, milestones, timeline, blockers. followup needs a §05 prompt.",
    "resolve": "{op:'resolve', target:'followups'|'questions', id, by, outcome|resolution} — sets resolved_at/by + outcome/resolution.",
    "lock": "{op:'lock', key, choice, rationale, by} — merges the lock into decisions[key], preserving authored title/context/choices.",
    "move": "{op:'move', target:'sprint_item', slug, from, to} — index only; preserves item metadata.",
    "create": "edit_plan(..., expected_version=0, create=True) on a NEW slug → writes a template then applies ops.",
}


def _discovery_state_root(root: str | None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve() / "docs" / "state"
    return _state_root()


def _discover_project(project: str, root: str | None = None) -> dict[str, Any]:
    from reckon.serve import discover_plans

    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return {"inventory": [], "sprints": [], "milestones": []}
    return discover_plans(docs_dir, project, _discovery_state_root(root))


def _inventory_row(item: dict[str, Any]) -> dict[str, Any]:
    milestone = item.get("milestone", item.get("ms", "—"))
    modified = item.get("modified", item.get("last", ""))
    artifact_type = item.get("type", "plan")
    row = {
        "slug": item.get("slug"),
        "title": item.get("title"),
        "type": artifact_type,
        "owner": item.get("owner", ""),
        "summary": item.get("summary", ""),
        "href": item.get("href"),
        "last": modified,
        "modified": modified,
        "version": int(item.get("version", 0) or 0),
        "informs": list(item.get("informs") or []),
        "evidence_for": list(item.get("evidence_for") or []),
        "verifies": list(item.get("verifies") or []),
        "supersedes": list(item.get("supersedes") or []),
        "commits": list(item.get("commits") or []),
        "artifacts": list(item.get("artifacts") or []),
        "reviewed_at": item.get("reviewed_at", ""),
        "recorded_at": item.get("recorded_at", ""),
        "verdict": item.get("verdict", ""),
        "environment": item.get("environment", ""),
        "source": item.get("source", ""),
        "source_quality": item.get("source_quality", ""),
        "archived": item.get("archived", ""),
        "read": item.get("read", ""),
    }
    if artifact_type == "plan":
        row.update(
            {
                "status": item.get("status"),
                "impl": item.get("impl"),
                "ms": milestone,
                "milestone": milestone,
                "sprint": item.get("sprint"),
                "roi": item.get("roi"),
                "effort": item.get("effort"),
                "tier": item.get("tier", "sonnet"),
                "dec_open": int(item.get("dec_open", 0) or 0),
                "blockers": int(item.get("blockers", 0) or 0),
                "depends_on": list(item.get("depends_on") or []),
                "blocks": list(item.get("blocks") or []),
            }
        )
    return row


def _matches_search(item: dict[str, Any], search: str | None) -> bool:
    if not search:
        return True
    needle = search.strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        str(item.get(field, "") or "")
        for field in (
            "slug",
            "title",
            "summary",
            "owner",
            "type",
            "verdict",
            "environment",
            "source",
            "source_quality",
        )
    ).lower()
    return needle in haystack


def _filter_inventory(
    inventory: list[dict[str, Any]],
    *,
    status: str | None = None,
    doc_type: str | None = None,
    sprint: str | None = None,
    milestone: str | None = None,
    owner: str | None = None,
    search: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    filtered = []
    for item in inventory:
        if status and item.get("status") != status:
            continue
        raw_filter = doc_type.strip().lower() if isinstance(doc_type, str) else doc_type
        canonical_filter = "research" if raw_filter == "doc" else raw_filter
        if canonical_filter and item.get("type") != canonical_filter:
            continue
        if sprint and (item.get("sprint") or "") != sprint:
            continue
        if (
            milestone
            and (item.get("milestone", item.get("ms", "—")) or "—") != milestone
        ):
            continue
        if owner and (item.get("owner") or "") != owner:
            continue
        if not _matches_search(item, search):
            continue
        filtered.append(item)
    if limit is not None:
        filtered = filtered[: max(0, limit)]
    return filtered


def _rollup_counts(values: list[str]) -> dict[str, int]:
    return dict(Counter(values))


def _discovery_summary(
    plans: list[dict[str, Any]],
    followups: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    actionable = [item for item in plans if item.get("type", "plan") == "plan"]
    sprint_values = [plan.get("sprint") or "—" for plan in actionable]
    milestone_values = [
        plan.get("milestone") or plan.get("ms") or "—" for plan in actionable
    ]
    impl_values = [float(plan.get("impl", 0.0) or 0.0) for plan in actionable]
    return {
        "plans": len(actionable),
        "artifacts": len(plans),
        "sprints": len({sid for sid in sprint_values if sid != "—"}),
        "milestones": len({mid for mid in milestone_values if mid != "—"}),
        "open_followups": len(followups),
        "open_questions": len(questions),
        "open_decisions": sum(int(plan.get("dec_open", 0) or 0) for plan in actionable),
        "impl_mean": round(sum(impl_values) / len(impl_values), 3)
        if impl_values
        else 0.0,
        "by_status": _rollup_counts(
            [str(plan.get("status") or "draft") for plan in actionable]
        ),
        "by_type": _rollup_counts([str(plan.get("type") or "plan") for plan in plans]),
        "by_sprint": _rollup_counts(sprint_values),
        "by_milestone": _rollup_counts(milestone_values),
    }


def _finding(
    category: str,
    code: str,
    severity: str,
    message: str,
    *,
    slug: str | None = None,
    path: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "category": category,
        "code": code,
        "severity": severity,
        "message": message,
    }
    if slug is not None:
        row["slug"] = slug
    if path is not None:
        row["path"] = path
    if extra:
        row["extra"] = extra
    return row


def _sprint_item_slug(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("slug", "") or "")
    return ""


def _audit_sprint_findings(
    index_data: dict[str, Any], plans: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    sprints = list(index_data.get("sprints", []) or [])
    sprint_map = {
        sprint["id"]: sprint
        for sprint in sprints
        if isinstance(sprint, dict) and sprint.get("id")
    }
    active_ids = [
        sprint_id
        for sprint_id, sprint in sprint_map.items()
        if sprint.get("status") == "active"
    ]
    active_sprint_id = index_data.get("active_sprint_id")
    if len(active_ids) > 1:
        findings.append(
            _finding(
                "sprint",
                "multiple-active-sprints",
                "warn",
                f"multiple sprints are marked active: {', '.join(active_ids)}",
                extra={"active_ids": active_ids},
            )
        )
    if active_sprint_id and active_sprint_id not in sprint_map:
        findings.append(
            _finding(
                "sprint",
                "active-sprint-missing",
                "warn",
                f"active_sprint_id {active_sprint_id!r} does not match any sprint",
                extra={"active_sprint_id": active_sprint_id},
            )
        )
    if active_ids and active_sprint_id not in active_ids:
        findings.append(
            _finding(
                "sprint",
                "active-sprint-mismatch",
                "warn",
                "active_sprint_id does not match the sprint marked active",
                extra={
                    "active_sprint_id": active_sprint_id,
                    "active_status_ids": active_ids,
                },
            )
        )

    plan_map = {plan["slug"]: plan for plan in plans if plan.get("slug")}
    assigned: dict[str, str] = {}
    for sprint_id, sprint in sprint_map.items():
        for item in sprint.get("items", []) or []:
            slug = _sprint_item_slug(item)
            if not slug:
                continue
            if slug not in plan_map:
                findings.append(
                    _finding(
                        "sprint",
                        "sprint-item-missing-plan",
                        "warn",
                        f"sprint {sprint_id!r} contains {slug!r}, which is not a live plan slug",
                        slug=slug,
                        extra={"sprint_id": sprint_id},
                    )
                )
            prev = assigned.get(slug)
            if prev and prev != sprint_id:
                findings.append(
                    _finding(
                        "sprint",
                        "sprint-item-duplicate",
                        "warn",
                        f"{slug!r} appears in multiple sprints ({prev}, {sprint_id})",
                        slug=slug,
                        extra={"sprints": [prev, sprint_id]},
                    )
                )
            else:
                assigned[slug] = sprint_id

    for slug, plan in plan_map.items():
        plan_sprint = plan.get("sprint")
        if not plan_sprint:
            continue
        if plan_sprint not in sprint_map:
            findings.append(
                _finding(
                    "sprint",
                    "plan-sprint-missing",
                    "warn",
                    f"plan metadata assigns sprint {plan_sprint!r}, but that sprint is not defined",
                    slug=slug,
                    extra={"sprint_id": plan_sprint},
                )
            )
            continue
        assigned_sprint = assigned.get(slug)
        if assigned_sprint is None:
            findings.append(
                _finding(
                    "sprint",
                    "plan-sprint-missing-item",
                    "warn",
                    f"plan metadata assigns sprint {plan_sprint!r}, but the index sprint items do not include it",
                    slug=slug,
                    extra={"sprint_id": plan_sprint},
                )
            )
        elif assigned_sprint != plan_sprint:
            findings.append(
                _finding(
                    "sprint",
                    "plan-sprint-mismatch",
                    "warn",
                    f"plan metadata sprint {plan_sprint!r} disagrees with index sprint {assigned_sprint!r}",
                    slug=slug,
                    extra={"plan_sprint": plan_sprint, "index_sprint": assigned_sprint},
                )
            )
    return findings


def _list_plans(
    project: str,
    status: str | None = None,
    *,
    root: str | None = None,
) -> dict[str, Any]:
    """Return a lightweight index of plans for the project.

    Always uses live HTML meta-tag discovery so impl/status are never stale.
    Falls back to index.json inventory only when discovery is unavailable.
    Each entry includes the legacy summary fields plus richer discovery metadata.
    If status is given, filters to only plans matching that status value.
    """
    discovered = _discover_project(project, root)
    inventory = [_inventory_row(item) for item in discovered.get("inventory", [])]
    if not inventory:
        # Discovery unavailable — fall back to index.json (may be stale)
        data, _ = read_plan(project, "index", root)
        inventory = list(data.get("inventory", []))
    return {
        "project": project,
        "plans": _filter_inventory(inventory, status=status),
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
    forbidden = {"items", "id"}
    bad = forbidden & set(updates.keys())
    if bad:
        return {"ok": False, "error": f"use dedicated tools for: {sorted(bad)}"}

    valid_statuses = {"planned", "active", "done"}
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
      { slug (required), why_now, tier, done_when, status }
    Duplicate slugs within the same sprint are rejected.
    """
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
    valid_statuses = {"planned", "active", "done"}
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

    Preserves any item metadata (why_now, tier, done_when, etc.).
    """
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
    from reckon._schema import IndexData, PlanState

    try:
        if slug in ("index", "project"):
            IndexData.model_validate(working)
        else:
            state = PlanState.model_validate(working).validate_for_write()
            # Persist the validated canonical shape. This is what turns the
            # legacy ``doc`` alias into ``research`` and removes neutral
            # plan-only defaults from research/evidence writes.
            working.clear()
            working.update(state.canonical_dump())
    except ValueError as e:
        # Split the multi-line validate_for_write message into discrete lines;
        # pydantic ValidationError stringifies to a useful block too.
        msg = str(e)
        lines = [ln.strip(" -") for ln in msg.splitlines() if ln.strip()]
        return lines or [msg]
    return None


def _edit_plan(
    project: str,
    slug: str,
    ops: list[dict[str, Any]],
    expected_version: int,
    create: bool = False,
    checkout_path: str | None = None,
) -> dict[str, Any]:
    """Apply an ordered list of ops to one plan (or the project index), then
    schema-validate and write atomically with an optimistic-concurrency check.

    This is the single collapsed write tool. ``ops`` are applied IN ORDER to a
    working copy of the current state dict; the result is schema-validated; only
    then is it persisted (version-checked). On a bad op or a validation failure
    NOTHING is written and field-level errors are returned.

    Routing: slug="index" → project config (sprints/milestones/timeline/blockers,
    version = data._version); any other slug → a plan HTML (version = state.version).

    Verbs (the "op" key): set | append | resolve | lock | move. See read_plan(
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

    Returns { ok: True, project, slug, new_version, path[, warnings][, created] }
    on success — ``path`` is the ABSOLUTE file the write landed in, so a caller
    can reconcile deterministically (e.g. ``git -C <dir> status``).  On failure:
    { ok: False, error, ... } (op_error | schema_validation | version_conflict |
    create errors).
    """
    is_index = slug in ("index", "project")
    root = checkout_path  # alias: the tool-surface name vs the store-layer name

    # ── create path (plan slugs only) ──
    if create:
        if is_index:
            return {"ok": False, "error": "cannot create the index slug"}
        docs_dir = _docs_dir_for_project(project, root)
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
        html_file = docs_dir / f"{slug}.html"
        # Reject if a plan already exists at this slug (direct or via resolution).
        from reckon.serve import _resolve_plan_file

        if html_file.exists() or _resolve_plan_file(docs_dir, slug) is not None:
            return {
                "ok": False,
                "error": f"plan {slug!r} already exists — drop create=True to edit it",
            }
        if expected_version != 0:
            return {"ok": False, "error": "create requires expected_version=0"}
        html_file.write_text(new_plan_html(project, slug), encoding="utf-8")
        created_file = html_file  # cleaned up below if the create then fails
    else:
        created_file = None

    # ── read current state (after any template write) ──
    cur_data, cur_version = read_plan(project, slug, root)
    if not create and not cur_data and not is_index:
        # An empty plan dict for a non-index slug means the HTML file is absent.
        from reckon.serve import _resolve_plan_file

        docs_dir = _docs_dir_for_project(project, root)
        if docs_dir is None or _resolve_plan_file(docs_dir, slug) is None:
            return {
                "ok": False,
                "error": f"plan {slug!r} not found — pass create=True to create it",
            }
    if expected_version != cur_version:
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        return _conflict_response(
            VersionConflict(expected_version, cur_version, cur_data)
        )

    # ── apply ops to a working copy ──
    import copy

    working = copy.deepcopy(cur_data)
    try:
        warnings = apply_ops(working, ops or [], is_index)
    except OpError as e:
        # A failed create must leave NO trace — drop the just-written stub so the
        # contract clause "on failure → no write" holds and a retry is unblocked.
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        return {"ok": False, "error": "op_error", "detail": str(e)}

    # ── schema-validate the working dict (reject on failure, write nothing) ──
    errors = _validate_working(slug, working)
    if errors:
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        return {"ok": False, "error": "schema_validation", "details": errors}

    # ── persist the working DICT via the version-checked atomic write ──
    try:
        new_version = write_plan(project, slug, working, cur_version, root)
    except VersionConflict as e:
        return _conflict_response(e)

    result: dict[str, Any] = {
        "ok": True,
        "project": project,
        "slug": slug,
        "new_version": new_version,
        "path": _written_path(project, slug, root),
    }
    if warnings:
        result["warnings"] = warnings
    if create:
        result["created"] = True
    return result


def _written_path(project: str, slug: str, root: str | None) -> str | None:
    """Best-effort absolute path of the file edit_plan just wrote.

    For index/project slugs → the JSON state file; for plan slugs → the resolved
    HTML file.  Returned to callers so they can reconcile the write
    deterministically (e.g. ``git -C <dir> status`` in the right checkout).
    Never raises — returns None if the path cannot be resolved.
    """
    from reckon._store import _resolve_html_file, state_path

    try:
        if slug in ("index", "project"):
            return str(state_path(project, slug, root))
        hit = _resolve_html_file(project, slug, root)
        return str(hit) if hit is not None else None
    except Exception:  # noqa: BLE001 — path reporting must never fail the write
        return None


# ── audit — plan-schema conformance audit (warn half; never mutates) ────────


def _audit(project: str, checkout_path: str | None = None) -> dict[str, Any]:
    """Audit every plan in a project against the PlanState schema (the WARN half
    of reject-write-warn-doctor) and recompute the index rollups.

    For each plan HTML, parse it leniently then run validate_for_write semantics
    NON-RAISINGLY, collecting any messages. Recomputes sprint/milestone/projects
    rollups in the response (inventory[] stays synthesised live — not persisted).
    Returns { project, checked, conformant, violations:[{slug, errors}],
    rollups_recomputed: True, reindexed: False }.

    WARN/report ONLY — this NEVER mutates a plan or writes index.json. (Distinct
    from the CLI `reckon doctor`, which checks infra/skills/mounts, not schema.)
    With ``checkout_path``, the audit runs against that checkout's docs/state
    instead of the mounts-registered main checkout.
    """
    from reckon import _plan_html
    from reckon.doccheck import audit_lifecycle, audit_links
    from reckon.serve import _ARCHIVE_DIR, _NON_PLAN_DIRS, _NON_PLAN_FILES

    docs_dir = _docs_dir_for_project(project, checkout_path)
    if docs_dir is None:
        hint = (
            f"check checkout_path {checkout_path!r} contains a docs/ dir"
            if checkout_path is not None
            else "check mounts.json"
        )
        return {
            "ok": False,
            "error": f"no docs dir for project {project!r} — {hint}",
        }

    checked = 0
    violations: list[dict[str, Any]] = []
    html_files: list[Path] = []
    for html_file in sorted(docs_dir.rglob("*.html")):
        rel = html_file.relative_to(docs_dir)
        if any(part in _NON_PLAN_DIRS for part in rel.parts[:-1]):
            continue
        if _ARCHIVE_DIR in rel.parts[:-1]:
            continue
        if html_file.name in _NON_PLAN_FILES:
            continue
        html_files.append(html_file)
        try:
            text = html_file.read_text(encoding="utf-8", errors="replace")
            state = _plan_html.from_html(text)
        except Exception as e:  # noqa: BLE001 — audit must not crash on one bad file
            violations.append({"slug": html_file.stem, "errors": [f"parse error: {e}"]})
            checked += 1
            continue
        slug = state.slug or html_file.stem
        checked += 1
        try:
            state.validate_for_write()
        except ValueError as e:
            lines = [ln.strip(" -") for ln in str(e).splitlines() if ln.strip()]
            # Drop the leading "PlanState.validate_for_write failed:" header line.
            lines = [ln for ln in lines if not ln.endswith("failed:")]
            violations.append({"slug": slug, "errors": lines})

    plans = _filter_inventory(
        [
            _inventory_row(item)
            for item in _discover_project(project, checkout_path).get("inventory", [])
        ]
    )
    plan_lookup = {plan["slug"]: plan for plan in plans if plan.get("slug")}
    followups = list_followups_across(project, unresolved_only=True, root=checkout_path)
    questions = list_questions_across(project, unresolved_only=True, root=checkout_path)
    index_data, _ = read_plan(project, "index", checkout_path)

    findings: list[dict[str, Any]] = []
    for artifact in plans:
        artifact_type = artifact.get("type", "plan")
        if artifact_type == "research" and not artifact.get("informs"):
            findings.append(
                _finding(
                    "provenance",
                    "unlinked-research",
                    "warn",
                    f"{artifact['slug']}: research does not declare informs",
                    slug=artifact.get("slug"),
                    path=(f"{artifact['href']}.html" if artifact.get("href") else None),
                )
            )
        if artifact_type == "evidence" and not (
            artifact.get("evidence_for") or artifact.get("verifies")
        ):
            findings.append(
                _finding(
                    "provenance",
                    "unlinked-evidence",
                    "warn",
                    f"{artifact['slug']}: evidence does not declare evidence_for or verifies",
                    slug=artifact.get("slug"),
                    path=(f"{artifact['href']}.html" if artifact.get("href") else None),
                )
            )
    try:
        for item in audit_lifecycle(project=project, docs_dir=docs_dir):
            severity = "error" if item.flag == "MISSING_IMPL" else "warn"
            findings.append(
                _finding(
                    "lifecycle",
                    item.flag,
                    severity,
                    f"{item.slug}: {item.flag} (age={item.age_days}d, impl={item.impl}, last={item.last_modified})",
                    slug=item.slug,
                    path=(
                        f"{plan_lookup[item.slug]['href']}.html"
                        if item.slug in plan_lookup
                        and plan_lookup[item.slug].get("href")
                        else None
                    ),
                    extra={
                        "age_days": item.age_days,
                        "impl": item.impl,
                        "last_modified": item.last_modified,
                    },
                )
            )
    except Exception:  # noqa: BLE001 — audit should degrade, not fail
        pass
    try:
        link_findings = audit_links(html_files, docs_dir, project=project)
        for path, path_findings in link_findings.items():
            rel = str(path.relative_to(docs_dir))
            slug = path.stem
            for item in path_findings:
                findings.append(
                    _finding(
                        "references",
                        item.code,
                        item.severity,
                        item.message,
                        slug=slug,
                        path=rel,
                    )
                )
    except Exception:  # noqa: BLE001 — audit should degrade, not fail
        pass
    # External (cross-project) refs: doccheck's per-file pass is corpus-local
    # by design, so qualified refs are resolved here, where mounts are known.
    from reckon._schema import parse_plan_ref

    for artifact in plans:
        for field in ("depends_on", "blocks"):
            for ref in artifact.get(field) or []:
                parsed = parse_plan_ref(ref)
                if parsed is None or not parsed.is_external(project):
                    continue
                resolved = _resolve_plan_ref(ref, project)
                if resolved.get("found"):
                    continue
                mounted = _docs_dir_for_project(parsed.project) is not None
                findings.append(
                    _finding(
                        "references",
                        "dangling-external-ref"
                        if mounted
                        else "unmounted-external-project",
                        "warn",
                        (
                            f"{artifact['slug']}: {field} external ref {ref!r} "
                            + (
                                "does not resolve in its mounted project"
                                if mounted
                                else "names a project absent from mounts.json"
                            )
                        ),
                        slug=artifact.get("slug"),
                    )
                )
    findings.extend(_audit_sprint_findings(index_data, plans))

    discovered = _discover_project(project, checkout_path)
    rollups = {
        "sprints": discovered.get("sprints", []),
        "milestones": discovered.get("milestones", []),
        "plans": sum(1 for item in plans if item.get("type", "plan") == "plan"),
        "artifacts": len(plans),
        "summary": _discovery_summary(plans, followups, questions),
    }
    finding_counts = {
        "total": len(findings),
        "by_severity": _rollup_counts([finding["severity"] for finding in findings]),
        "by_category": _rollup_counts([finding["category"] for finding in findings]),
        "by_code": _rollup_counts([finding["code"] for finding in findings]),
    }

    return {
        "project": project,
        "checked": checked,
        "conformant": checked - len(violations),
        "violations": violations,
        "findings": findings,
        "finding_counts": finding_counts,
        "rollups": rollups,
        "rollups_recomputed": True,
        "reindexed": False,
    }


# ── Register tools with SDK ────────────────────────────────────────────────
#
# Agent-facing MCP surface = read_plan + edit_plan + audit. The granular
# _funcs below remain for tests/internal use but are intentionally NOT
# registered (collapsed per the schema-and-tooling plan); full removal is a
# later cleanup. read_plan folds the 5 legacy reads (list_plans/list_projects/
# list_sprints/list_followups/list_questions) via its discovery + with_schema
# modes; edit_plan folds the granular mutators via its set/append/resolve/lock/
# move + create ops.

if mcp is not None:
    read_plan_tool = mcp.tool()(_read_plan)
    edit_plan_tool = mcp.tool()(_edit_plan)
    audit_tool = mcp.tool()(_audit)


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
