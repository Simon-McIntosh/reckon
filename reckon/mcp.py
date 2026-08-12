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
from typing import Any, Literal

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
    replace_plan_text,
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
            "before edit_plan — writes are rejected if "
            "expected_version doesn't match the current plan version. Use "
            "roadmap before execution or relationship/sprint changes."
        ),
    )
else:
    mcp = None  # type: ignore[assignment]


def _resource_reference(
    project: str,
    slug: str,
    doc_type: str | None,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Build the typed affected-resource identity used by write responses."""

    from reckon.resources import canonical_type

    resource_type = (
        canonical_type(doc_type)
        if doc_type
        else ("project" if slug in {"index", "project"} else "plan")
    )
    resource_id = "project" if resource_type == "project" and slug == "index" else slug
    return {
        "project": project,
        "type": resource_type,
        "id": resource_id,
        "archived": False,
        "title": title or resource_id,
    }


def _resource_title(data: dict[str, Any], fallback: str) -> str:
    """Choose the human label for one write response."""

    return str(
        data.get("title")
        or data.get("theme")
        or data.get("name")
        or data.get("summary")
        or fallback
    )


def _conflict_response(
    exc: VersionConflict,
    *,
    project: str | None = None,
    slug: str | None = None,
    doc_type: str | None = None,
    operation: str = "edit",
) -> dict[str, Any]:
    title = _resource_title(exc.current_data, slug or "resource")
    result: dict[str, Any] = {
        "ok": False,
        "error": "version_conflict",
        "message": (
            f"Could not {operation} {title}: expected version {exc.expected}, "
            f"but the current version is {exc.current}."
        ),
        "operation": operation,
        "expected_version": exc.expected,
        "current_version": exc.current,
        "hint": "Re-read the plan with reckon.read_plan to get the current version, then retry.",
    }
    if project is not None and slug is not None:
        result["resource"] = _resource_reference(project, slug, doc_type, title=title)
    return result


def _edit_success_response(
    *,
    project: str,
    slug: str,
    doc_type: str | None,
    new_version: int,
    data: dict[str, Any] | None = None,
    created: bool = False,
) -> dict[str, Any]:
    """Translate one successful edit into human and machine-readable forms."""

    operation = "create" if created else "edit"
    title = _resource_title(data or {}, slug)
    resource = _resource_reference(project, slug, doc_type, title=title)
    verb = "Created" if created else "Updated"
    return {
        "ok": True,
        "message": (f"{verb} {resource['type']} {title} to version {new_version}."),
        "operation": operation,
        "resource": resource,
        "project": project,
        "slug": slug,
        "new_version": new_version,
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
    resource: dict[str, Any] | None = None,
    view: str | None = None,
    cursor: str | None = None,
    include_prompts: bool = False,
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

      read_plan(resource={project, type, id[, archived]}[, view=...])
          → a progressive typed response. ``summary`` is the default and keeps
            identity, version, human state, blockers/open decisions, and the next
            action compact. ``detail`` adds current metadata and unresolved
            workflow, ``history`` paginates prior workflow, ``raw`` returns the
            lossless storage state, and ``schema`` describes the response plus
            the selected resource's storage schema. Full followup prompts require
            ``view="detail", include_prompts=True``.

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
    if resource is not None or view is not None:
        return _read_plan_view(
            project=project,
            slug=slug,
            checkout_path=checkout_path,
            doc_type=doc_type,
            resource=resource,
            view=view,
            cursor=cursor,
            limit=limit,
            include_prompts=include_prompts,
            status=status,
            sprint=sprint,
            milestone=milestone,
            owner=owner,
            search=search,
            include_followups=include_followups,
            include_questions=include_questions,
        )

    # ── projects-list mode ──
    if project is None or project == "*":
        return _list_projects()

    from reckon.project_state import ProjectStateError

    # ── discovery mode (no slug) ──
    if slug is None:
        try:
            discovered = _discover_project(project, checkout_path)
        except ProjectStateError as exc:
            return {
                "ok": False,
                "error": "project_state_error",
                "project": project,
                "detail": str(exc),
            }
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
            "blockers": discovered.get("blockers", []),
            "timeline": discovered.get("timeline", []),
            "active_sprint_id": active_sprint_id,
            "source_format": discovered.get("source_format", "legacy-index"),
            "resource_versions": discovered.get("resource_versions", {}),
            "summary": _discovery_summary(plans, followups, questions),
        }

    # ── single-plan mode (original shape) ──
    try:
        if doc_type is None:
            data, version = read_plan(project, slug, checkout_path)
        else:
            data, version = read_plan(
                project, slug, checkout_path, artifact_type=doc_type
            )
    except ProjectStateError as exc:
        return {
            "ok": False,
            "error": "project_state_error",
            "project": project,
            "slug": slug,
            "doc_type": doc_type,
            "detail": str(exc),
        }
    if slug in ("index", "project") and doc_type is None:
        from reckon.capability import map_legacy_capabilities

        index_warnings: list[str] = []
        normalised_sprints: list[Any] = []
        for sprint_record in data.get("sprints", []):
            if not isinstance(sprint_record, dict):
                normalised_sprints.append(sprint_record)
                continue
            sprint_copy = dict(sprint_record)
            normalised_items: list[Any] = []
            for item in sprint_record.get("items", []):
                if not isinstance(item, dict):
                    normalised_items.append(item)
                    continue
                mapped, warnings = map_legacy_capabilities(
                    item,
                    context=(
                        f"sprint {sprint_record.get('id') or '<no-id>'} "
                        f"item {item.get('slug') or '<no-slug>'}"
                    ),
                )
                normalised_items.append(mapped)
                index_warnings.extend(warnings)
            sprint_copy["items"] = normalised_items
            normalised_sprints.append(sprint_copy)
        data = {**data, "sprints": normalised_sprints}
        if index_warnings:
            data["compatibility_warnings"] = index_warnings
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


def _read_archived_resource(
    project: str,
    slug: str,
    doc_type: str,
    checkout_path: str | None,
) -> tuple[dict[str, Any], int]:
    """Read exactly one archived typed artifact without live-resource ambiguity."""

    from reckon import _plan_html
    from reckon.resources import canonical_type, resource_map

    docs_dir = _docs_dir_for_project(project, checkout_path)
    if docs_dir is None:
        return {}, 0
    key = (canonical_type(doc_type), slug, True)
    resource = resource_map(docs_dir, project, include_archived=True).get(key)
    if resource is None:
        return {}, 0
    data = _plan_html.read_state(
        resource.path.read_text(encoding="utf-8", errors="replace")
    )
    return data, int(data.get("version", 0) or 0)


def _read_legacy_project_resource(
    project: str,
    resource_type: str,
    resource_id: str,
    checkout_path: str | None,
) -> tuple[dict[str, Any], int]:
    """Project one named resource from a canonical legacy aggregate index."""

    legacy = _read_plan(
        project=project,
        slug="index",
        checkout_path=checkout_path,
    )
    if legacy.get("ok") is False:
        return {}, 0
    aggregate = legacy.get("data") or {}
    version = int(legacy.get("version", 0) or 0)
    data: dict[str, Any] = {}
    collection = {
        "sprint": "sprints",
        "milestone": "milestones",
        "blocker": "blockers",
    }.get(resource_type)
    if collection is not None:
        data = next(
            (
                dict(item)
                for item in aggregate.get(collection) or []
                if isinstance(item, dict) and item.get("id") == resource_id
            ),
            {},
        )
    elif resource_type == "timeline" and resource_id == "timeline":
        data = {
            "id": "timeline",
            "events": list(aggregate.get("timeline") or []),
        }
    elif resource_type == "project" and resource_id == "project":
        rows = aggregate.get("projects") or []
        data = (
            dict(rows[0])
            if rows and isinstance(rows[0], dict)
            else {"project": project}
        )
        data.setdefault("project", project)
    if data:
        data["type"] = resource_type
        data["version"] = version
        data["compatibility_warnings"] = [
            "Projected from the legacy aggregate index; named writes require "
            "the legacy index path until distributed activation."
        ]
    return data, version


def _read_plan_view(
    *,
    project: str | None,
    slug: str | None,
    checkout_path: str | None,
    doc_type: str | None,
    resource: dict[str, Any] | None,
    view: str | None,
    cursor: str | None,
    limit: int | None,
    include_prompts: bool,
    status: str | None,
    sprint: str | None,
    milestone: str | None,
    owner: str | None,
    search: str | None,
    include_followups: bool,
    include_questions: bool,
) -> dict[str, Any]:
    """Route opt-in progressive reads without changing the legacy call path."""

    from reckon.mcp_views import (
        ResourceSelector,
        ViewRequestError,
        discovery_view,
        error_response,
        normalize_selector,
        normalize_view,
        resource_view,
        storage_schema_for,
    )

    selector: ResourceSelector | None = None
    try:
        selected_view = normalize_view(view)
        if resource is not None:
            selector = normalize_selector(resource, fallback_project=project)
            if project not in (None, selector.project):
                raise ViewRequestError(
                    "invalid_resource",
                    "project and resource.project must name the same project.",
                )
        elif slug is None:
            if not project or project == "*":
                raise ViewRequestError(
                    "invalid_resource",
                    "A project is required for progressive discovery views.",
                )
            raw = _read_plan(
                project=project,
                checkout_path=checkout_path,
                status=status,
                doc_type=doc_type,
                sprint=sprint,
                milestone=milestone,
                owner=owner,
                search=search,
                limit=None,
                include_followups=include_followups,
                include_questions=include_questions,
            )
            return discovery_view(
                project,
                raw,
                view=selected_view,
                cursor=cursor,
                limit=limit,
                include_prompts=include_prompts,
                storage_schema=storage_schema_for("project"),
                op_vocab=_OP_VOCAB,
                dos_donts=_DOS_DONTS,
            )
        else:
            if not project or project == "*":
                raise ViewRequestError(
                    "invalid_resource",
                    "A project is required for progressive resource views.",
                )
            inferred_type = doc_type or (
                "project" if slug in {"index", "project"} else "plan"
            )
            selector = normalize_selector(
                {
                    "project": project,
                    "type": inferred_type,
                    "id": "project" if slug == "index" else slug,
                    "archived": False,
                }
            )

        if selector.archived and selector.type not in {
            "plan",
            "research",
            "evidence",
        }:
            raise ViewRequestError(
                "invalid_resource",
                f"{selector.type} resources do not have archived typed identities.",
            )

        if selector.type == "project" and selected_view != "raw":
            raw_discovery = _read_plan(
                project=selector.project,
                checkout_path=checkout_path,
                limit=None,
                include_followups=include_followups,
                include_questions=include_questions,
            )
            result = discovery_view(
                selector.project,
                raw_discovery,
                view=selected_view,
                cursor=cursor,
                limit=limit,
                include_prompts=include_prompts,
                storage_schema=storage_schema_for("project"),
                op_vocab=_OP_VOCAB,
                dos_donts=_DOS_DONTS,
            )
            result["resource"] = selector.as_dict()
            return result

        if selector.archived:
            data, version = _read_archived_resource(
                selector.project,
                selector.id,
                selector.type,
                checkout_path,
            )
            deps: list[dict[str, Any]] = []
        else:
            legacy = _read_plan(
                project=selector.project,
                slug=selector.id,
                checkout_path=checkout_path,
                doc_type=selector.type,
            )
            if legacy.get("ok") is False:
                detail = str(legacy.get("detail") or "")
                if (
                    selector.type
                    in {"sprint", "milestone", "blocker", "timeline", "project"}
                    and "distributed_resource_inactive" in detail
                ):
                    data, version = _read_legacy_project_resource(
                        selector.project,
                        selector.type,
                        selector.id,
                        checkout_path,
                    )
                    deps = []
                else:
                    return error_response(
                        legacy.get("error", "read_error"),
                        detail or "The resource could not be read.",
                        selector=selector,
                    )
            else:
                data = legacy.get("data") or {}
                version = int(legacy.get("version", 0) or 0)
                deps = list(legacy.get("deps") or [])

        if not data:
            return error_response(
                "not_found",
                (
                    f"{selector.type} resource {selector.project}:"
                    f"{selector.id} was not found."
                ),
                selector=selector,
                hint="Check the typed identity and archived flag.",
            )

        if selector.type == "sprint" and selected_view in {"summary", "detail"}:
            discovered = _discover_project(selector.project, checkout_path)
            composed = next(
                (
                    item
                    for item in discovered.get("sprints", [])
                    if isinstance(item, dict) and item.get("id") == selector.id
                ),
                None,
            )
            if composed is not None:
                data = composed
        elif selector.type == "plan" and selected_view in {"summary", "detail"}:
            discovered = _discover_project(selector.project, checkout_path)
            inventory_plan = next(
                (
                    item
                    for item in discovered.get("inventory", [])
                    if isinstance(item, dict)
                    and item.get("type", "plan") == "plan"
                    and item.get("slug") == selector.id
                ),
                None,
            )
            if inventory_plan is not None:
                explicit_blockers = [
                    item.get("id")
                    for item in inventory_plan.get("blocking", [])
                    if isinstance(item, dict)
                    and item.get("kind") == "explicit"
                    and item.get("id")
                ]
                if explicit_blockers:
                    data = {**data, "blocked_by": explicit_blockers}

        return resource_view(
            selector,
            version,
            data,
            view=selected_view,
            deps=deps,
            cursor=cursor,
            limit=limit,
            include_prompts=include_prompts,
            storage_schema=storage_schema_for(selector.type),
            op_vocab=_OP_VOCAB,
            dos_donts=_DOS_DONTS,
        )
    except ViewRequestError as exc:
        return error_response(
            exc.code,
            exc.message,
            selector=selector,
            hint=exc.hint,
        )
    except Exception as exc:  # noqa: BLE001 — tool errors must remain structured
        return error_response(
            "read_error",
            str(exc),
            selector=selector,
            hint="Inspect the typed identity and project-state audit.",
        )


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
        "give every followup one /reckon-ship invocation line; store guidance in the plan.",
        "slug='index' is a composed compatibility read; edit named project resources with doc_type.",
        "use canonical artifact types plan, research, or evidence; doc reads as research.",
        "use project:slug or project:slug#stage provenance refs; unqualified same-project refs remain valid.",
        "depends_on/blocks take the same grammar: bare slug = local, project:slug = external; read_plan(project, slug) resolves them in its deps list.",
        "use depends_on only for executable prerequisites; research, evidence, and specifications use informs.",
        "landed/outcome records carry evidence_for naming the plan(s) whose execution they record — the plan-to-generated-evidence back-link; informs is reserved for INPUTS that feed future work.",
        "use roadmap for pending work, completion, ready/blocked sets, sprint order, critical paths, and wiring findings.",
        "use edit_plan mode='text' for one exact version-safe authored HTML replacement; use mode='state' for structured ops.",
    ],
    "dont": [
        "never set plan-version yourself — the server owns it.",
        "off-enum status/roi/effort/type or capability requirements are rejected at the write boundary.",
        "research/evidence cannot carry meaningful plan-only workflow or scheduling fields.",
        "distributed index writes are rejected with legacy_index_read_only guidance.",
        "create=True on an existing plan, or a normal edit on a missing plan, is rejected.",
        "never execute through an error-level roadmap wiring finding; repair and rescan first.",
    ],
}

#: The edit_plan op vocabulary, inlined for the context injector.
_OP_VOCAB = {
    "set": "{op:'set', path:'<dotted>', value:<any>} — artifact scalars, decisions.<key>.<field>, or followups.<id>.prompt; or one top-level field on a selected sprint/milestone/blocker/project resource. impl clamps to 0..1 and is plan-only.",
    "append": "{op:'append', target:'<collection>', item:<obj|str>[, section][, key]} — plan followups/research/questions/comments/decisions; sprint items; timeline events. followup prompt is one /reckon-ship invocation line.",
    "resolve": "{op:'resolve', target:'followups'|'questions', id, by, outcome|resolution} — sets resolved_at/by + outcome/resolution.",
    "lock": "{op:'lock', key, choice, rationale, by} — merges the lock into decisions[key], preserving authored title/context/choices.",
    "move": "{op:'move', target:'sprint_item', slug, to, to_version} — selected source sprint; checks both versions and preserves item metadata.",
    "create": "edit_plan(..., expected_version=0, create=True) on a NEW slug → creates a plan or named project resource selected by doc_type.",
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
        "resource_id": item.get("resource_id"),
        "title": item.get("title"),
        "type": artifact_type,
        "owner": item.get("owner", ""),
        "summary": item.get("summary", ""),
        "href": item.get("href"),
        "canonical_href": item.get("canonical_href"),
        "legacy": bool(item.get("legacy", False)),
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
                "workflow_status": item.get(
                    "workflow_status",
                    item.get("status"),
                ),
                "effective_status": item.get(
                    "effective_status",
                    item.get("status"),
                ),
                "impl": item.get("impl"),
                "ms": milestone,
                "milestone": milestone,
                "sprint": item.get("sprint"),
                "roi": item.get("roi"),
                "effort": item.get("effort"),
                "capability": item.get("capability"),
                "tier": item.get("tier"),
                "dec_open": int(item.get("dec_open", 0) or 0),
                "blockers": int(item.get("blockers", 0) or 0),
                "blocking": list(item.get("blocking") or []),
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
    include_archived: bool = False,
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
        if item.get("archived") and not include_archived:
            continue
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
            [
                str(plan.get("effective_status") or plan.get("status") or "draft")
                for plan in actionable
            ]
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
    closed_sprint_statuses = {"done", "shipped", "archived"}
    terminal_plan_statuses = {
        "shipped",
        "done",
        "archived",
        "superseded",
        "abandoned",
        "historical",
    }
    assigned: dict[str, str] = {}
    for sprint_id, sprint in sprint_map.items():
        sprint_is_actionable = sprint.get("status") not in closed_sprint_statuses
        for item in sprint.get("items", []) or []:
            slug = _sprint_item_slug(item)
            if not slug:
                continue
            if sprint_is_actionable and slug not in plan_map:
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
            if sprint_is_actionable:
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
        if str(plan.get("status") or "").lower() in terminal_plan_statuses:
            continue
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
    """Apply a JSON merge-patch to the plan data blob.

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
    The prompt field is one ``/reckon-ship`` invocation line; the plan owns all
    semantic guidance.
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
      { slug (required), why_now, capability, done_when, status }
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

    Preserves any item metadata (why_now, capability, done_when, etc.).
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
    from reckon.capability import (
        from_legacy_tier,
        validate_capability,
    )

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
) -> dict[str, Any]:
    """Edit structured state or authored prose with version protection.

    ``mode='state'`` applies ``ops`` IN ORDER to a working copy, validates the
    resulting schema, and writes atomically. ``mode='text'`` replaces the one
    exact ``old_html`` occurrence with ``new_html`` and refuses any structured
    state change. Text mode accepts ``ops=None`` or an empty list and does not
    support ``create``. Both modes reject stale ``expected_version`` values.

    Routing: slug="index" → project config (sprints/milestones/timeline/blockers,
    version = data._version); any other slug → typed HTML selected by
    ``doc_type`` (version = state.version). Untyped edits retain compatibility
    only when the leaf slug identifies one live artifact unambiguously.

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
    if old_html is not None or new_html is not None:
        return {
            "ok": False,
            "error": "invalid_edit_request",
            "detail": "state mode does not accept old_html or new_html",
        }
    if ops is None:
        return {
            "ok": False,
            "error": "invalid_edit_request",
            "detail": "state mode requires an ops list",
        }

    is_index = slug in ("index", "project") and doc_type is None
    root = checkout_path  # alias: the tool-surface name vs the store-layer name
    from reckon._schema import TYPE_ENUM
    from reckon.resources import ResourceCollision, canonical_type, resolve_resource

    canonical_doc_type = canonical_type(doc_type) if doc_type else None
    from reckon.project_state import (
        RESOURCE_TYPES as PROJECT_RESOURCE_TYPES,
        LegacyIndexReadOnly,
        ProjectStateConflict,
        ProjectStateError,
        apply_resource_ops,
        resource_path,
    )

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
        except (ValueError, FileNotFoundError) as exc:
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
        from reckon.serve import _resolve_plan_file

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
        html_file.parent.mkdir(parents=True, exist_ok=True)
        html_file.write_text(new_plan_html(project, slug), encoding="utf-8")
        created_file = html_file  # cleaned up below if the create then fails
    else:
        created_file = None

    # ── read current state (after any template write) ──
    cur_data, cur_version = read_plan(project, slug, root, artifact_type=selected_type)
    if not create and not cur_data and not is_index:
        # An empty plan dict for a non-index slug means the HTML file is absent.
        from reckon.serve import _resolve_plan_file

        docs_dir = _docs_dir_for_project(project, root)
        if (
            docs_dir is None
            or _resolve_plan_file(docs_dir, slug, "plan", project=project) is None
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

    # ── persist the working DICT via the version-checked atomic write ──
    try:
        new_version = write_plan(
            project,
            slug,
            working,
            cur_version,
            root,
            artifact_type=selected_type,
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
    if warnings:
        result["warnings"] = warnings
    if create:
        result["created"] = True
    return result


def _edit_plan_prose(
    project: str,
    slug: str,
    old_html: str,
    new_html: str,
    expected_version: int,
    checkout_path: str | None = None,
    doc_type: str | None = None,
) -> dict[str, Any]:
    """Replace one exact authored HTML fragment with version protection.

    Use this for plan prose, tables, figures, and section bodies.  The old
    fragment must occur exactly once.  The operation refuses any change to
    plan metadata or ``data-reckon`` state; use ``edit_plan`` for structured
    fields.  Pair the version and ``checkout_path`` with the preceding raw
    ``read_plan`` call.
    """

    try:
        new_version, path = replace_plan_text(
            project,
            slug,
            old_html,
            new_html,
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
    create: bool = False,
    checkout_path: str | None = None,
    doc_type: str | None = None,
) -> dict[str, Any]:
    """Edit one Reckon resource through a version-safe state or text mode.

    Use ``mode='state'`` with ``ops`` for validated structured changes. Use
    ``mode='text'`` with ``old_html`` and ``new_html`` for one exact authored
    HTML replacement. Read the same resource first and pass its version as
    ``expected_version``; worktree callers must reuse the same
    ``checkout_path`` on both calls.
    """

    return _edit_plan(
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
    )


def _roadmap(
    project: str,
    checkout_path: str | None = None,
    sprint: str | None = None,
    max_paths: int = 5,
) -> dict[str, Any]:
    """Scan plan dependencies and return executable work plus graph health.

    The response contains every pending plan in scope, ready, blocked, and
    deferred sets,
    lifecycle and implementation percentages, ordered sprint rollups, the
    weighted critical path, alternative open paths, and wiring findings.
    ``project='*'`` returns the same report for every mounted project plus a
    portfolio rollup.  ``checkout_path`` is accepted for a single project and
    follows the same worktree-routing contract as ``read_plan``.
    """

    from reckon.roadmap import build_roadmap

    if max_paths < 1 or max_paths > 50:
        return {
            "ok": False,
            "error": "invalid_max_paths",
            "detail": "max_paths must be between 1 and 50",
        }
    if project == "*":
        if checkout_path is not None:
            return {
                "ok": False,
                "error": "portfolio_checkout_path_unsupported",
                "detail": "select one project when using checkout_path",
            }
        listed = _list_projects()
        reports = [
            _roadmap(
                str(item["name"]),
                sprint=sprint,
                max_paths=max_paths,
            )
            for item in listed.get("projects", [])
            if isinstance(item, dict) and item.get("name")
        ]
        valid = [report for report in reports if report.get("ok", True)]
        plan_count = sum(
            report.get("completion", {}).get("plans", 0) for report in valid
        )
        completed = sum(
            report.get("completion", {}).get("completed", 0) for report in valid
        )
        implementation_points = sum(
            report.get("completion", {}).get("implementation_pct", 0.0)
            * report.get("completion", {}).get("plans", 0)
            for report in valid
        )
        return {
            "project": "*",
            "portfolio": {
                "projects": len(valid),
                "plans": plan_count,
                "completed": completed,
                "lifecycle_completion_pct": round(100 * completed / plan_count, 1)
                if plan_count
                else 0.0,
                "implementation_pct": round(implementation_points / plan_count, 1)
                if plan_count
                else 0.0,
                "ready": sum(len(report.get("ready_now", [])) for report in valid),
                "blocked": sum(len(report.get("blocked", [])) for report in valid),
                "deferred": sum(len(report.get("deferred", [])) for report in valid),
                "wiring_findings": sum(
                    len(report.get("wiring_findings", [])) for report in valid
                ),
            },
            "projects": reports,
        }

    try:
        discovered = _discover_project(project, checkout_path)
        index_data, _version = read_plan(project, "index", checkout_path)
        project_rows = index_data.get("projects") or []
        project_manifest = (
            project_rows[0]
            if project_rows and isinstance(project_rows[0], dict)
            else {}
        )
        return build_roadmap(
            project,
            [_inventory_row(item) for item in discovered.get("inventory", [])],
            list(discovered.get("sprints", [])),
            active_sprint_id=(
                discovered.get("active_sprint_id") or index_data.get("active_sprint_id")
            ),
            sprint_id=sprint,
            max_paths=max_paths,
            project_manifest=project_manifest,
        )
    except Exception as exc:  # noqa: BLE001 — MCP errors stay structured
        return {
            "ok": False,
            "error": "roadmap_error",
            "project": project,
            "detail": str(exc),
        }


def _crew(
    project: str,
    view: str = "summary",
    checkout_path: str | None = None,
) -> dict[str, Any]:
    """Read crew state: resolved routing, live runs, the ledger, or a summary.

    Read-only, and deliberately one tool over four views rather than four tools.
    ``ledger`` and ``summary`` read the project's committed run records —
    ``<repo>/docs/state/<project>/crew.json``, the durable half; ``live`` reads
    the never-committed pointers of runs still in flight, each carrying the
    classification :func:`reckon.crew.recover` would give it; ``flight`` reports
    the resolved routing config with the layer that supplied every value.

    ``checkout_path`` follows the same worktree-routing contract as
    ``read_plan``: with it, the ledger and the routing project layer resolve
    inside that checkout instead of the registered main one.
    """
    from reckon import crew as crew_module
    from reckon import flight as flight_module
    from reckon import ledger as ledger_module

    if view not in ("summary", "flight", "live", "ledger"):
        return {
            "ok": False,
            "error": "invalid_view",
            "detail": "view must be summary, flight, live or ledger",
        }
    try:
        if view == "flight":
            return {
                "ok": True,
                "project": project,
                "view": view,
                **flight_module.flight_report(project, checkout_path=checkout_path),
            }
        if view == "live":
            runs = [
                crew_module.classify_pointer(record)
                for record in crew_module.list_live()
                if str(record.get("project") or "") == project
            ]
            return {"ok": True, "project": project, "view": view, "runs": runs}
        if view == "ledger":
            data, version = ledger_module.load(project, checkout_path)
            return {
                "ok": True,
                "project": project,
                "view": view,
                "version": version,
                "path": str(ledger_module.ledger_path(project, checkout_path)),
                **data,
            }
        return {
            "ok": True,
            "project": project,
            "view": view,
            **ledger_module.summary(project, root=checkout_path),
        }
    except (
        ledger_module.LedgerError,
        crew_module.CrewError,
        flight_module.FlightConfigError,
    ) as exc:
        return {
            "ok": False,
            "error": "crew_error",
            "project": project,
            "detail": str(exc),
        }


def _written_path(
    project: str,
    slug: str,
    root: str | None,
    doc_type: str | None = None,
) -> str | None:
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
        hit = _resolve_html_file(project, slug, root, doc_type)
        return str(hit) if hit is not None else None
    except Exception:  # noqa: BLE001 — path reporting must never fail the write
        return None


# ── audit — plan-schema conformance audit (warn half; never mutates) ────────


def _audit(
    project: str,
    checkout_path: str | None = None,
    view: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
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
    instead of the mounts-registered main checkout. Pass ``view="summary"`` for
    compact counts, ``view="detail"`` for paginated findings, or ``view="raw"``
    for the exact legacy audit payload. Omitting ``view`` preserves the legacy
    response unchanged.
    """
    if view is not None:
        from reckon.mcp_views import ViewRequestError, audit_view, error_response

        raw = _audit(project, checkout_path)
        try:
            return audit_view(
                project,
                raw,
                view=view,
                cursor=cursor,
                limit=limit,
            )
        except ViewRequestError as exc:
            return error_response(exc.code, exc.message, hint=exc.hint)

    from reckon import _plan_html
    from reckon.doccheck import audit_lifecycle, audit_links
    from reckon.resources import ResourceCollision, identify_resource

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
    compatibility_records: list[tuple[str, str, str]] = []
    resource_collisions: list[tuple[str, str, str]] = []
    invalid_resources: list[tuple[str, str]] = []
    html_files: list[Path] = []
    seen_resources: dict[tuple[str, str], Path] = {}
    for html_path in sorted(docs_dir.rglob("*.html")):
        try:
            resource = identify_resource(docs_dir, html_path, project)
        except ResourceCollision as exc:
            invalid_resources.append((str(html_path.relative_to(docs_dir)), str(exc)))
            continue
        if resource is None or resource.archived:
            continue
        if resource.type not in {"plan", "research", "evidence"}:
            continue
        html_file = resource.path
        resource_key = (resource.type, resource.slug)
        existing_path = seen_resources.get(resource_key)
        if existing_path is not None:
            resource_collisions.append(
                (
                    resource.identity.key,
                    str(existing_path.relative_to(docs_dir)),
                    str(html_file.relative_to(docs_dir)),
                )
            )
        else:
            seen_resources[resource_key] = html_file
        html_files.append(html_file)
        try:
            text = html_file.read_text(encoding="utf-8", errors="replace")
            state = _plan_html.from_html(text)
        except Exception as e:  # noqa: BLE001 — audit must not crash on one bad file
            violations.append({"slug": html_file.stem, "errors": [f"parse error: {e}"]})
            checked += 1
            continue
        slug = state.slug or html_file.stem
        for warning in state.compatibility_warnings:
            compatibility_records.append(
                (slug, str(html_file.relative_to(docs_dir)), warning)
            )
        checked += 1
        try:
            state.validate_for_write()
        except ValueError as e:
            lines = [ln.strip(" -") for ln in str(e).splitlines() if ln.strip()]
            # Drop the leading "PlanState.validate_for_write failed:" header line.
            lines = [ln for ln in lines if not ln.endswith("failed:")]
            violations.append({"slug": slug, "errors": lines})

    findings: list[dict[str, Any]] = []
    from reckon.project_state import audit_project_state

    project_state_findings = audit_project_state(docs_dir, project)
    if project_state_findings:
        for item in project_state_findings:
            findings.append(
                _finding(
                    "project-state",
                    item["code"],
                    item["severity"],
                    item["message"],
                )
            )
        return {
            "project": project,
            "checked": checked,
            "conformant": max(0, checked - len(violations)),
            "violations": violations,
            "findings": findings,
            "summary": {
                "errors": sum(
                    1 for item in findings if item.get("severity") == "error"
                ),
                "warnings": 0,
            },
            "ok": False,
        }

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

    for resource_id, first_path, second_path in resource_collisions:
        findings.append(
            _finding(
                "resources",
                "duplicate-resource-identity",
                "error",
                f"{resource_id} resolves to both {first_path} and {second_path}",
                path=second_path,
            )
        )
    for path, message in invalid_resources:
        findings.append(
            _finding(
                "resources",
                "invalid-resource-path",
                "error",
                message,
                path=path,
            )
        )
    for slug, path, warning in compatibility_records:
        findings.append(
            _finding(
                "compatibility",
                "legacy-capability-tier",
                "warn",
                warning,
                slug=slug,
                path=path,
            )
        )
    for sprint_record in index_data.get("sprints", []):
        if not isinstance(sprint_record, dict):
            continue
        for item in sprint_record.get("items", []):
            if (
                not isinstance(item, dict)
                or not item.get("tier")
                or item.get("capability")
            ):
                continue
            findings.append(
                _finding(
                    "compatibility",
                    "legacy-capability-tier",
                    "warn",
                    (
                        f"sprint {sprint_record.get('id') or '<no-id>'} item "
                        f"{item.get('slug') or '<no-slug>'}: legacy tier maps "
                        "on read; persist capability explicitly to migrate"
                    ),
                    slug=item.get("slug"),
                    path="state/index.json",
                )
            )
    for artifact in plans:
        artifact_type = artifact.get("type", "plan")
        if (
            artifact_type == "plan"
            and artifact.get("workflow_status", artifact.get("status")) == "blocked"
            and not artifact.get("blocking")
        ):
            findings.append(
                _finding(
                    "lifecycle",
                    "orphaned-blocked-status",
                    "warn",
                    (
                        f"{artifact['slug']}: persisted blocked status has no "
                        "unresolved dependency or explicit blocker reference"
                    ),
                    slug=artifact.get("slug"),
                    path=(f"{artifact['href']}.html" if artifact.get("href") else None),
                )
            )
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
    from reckon.roadmap import build_roadmap

    discovered = _discover_project(project, checkout_path)
    project_rows = index_data.get("projects") or []
    roadmap = build_roadmap(
        project,
        plans,
        list(discovered.get("sprints", [])),
        active_sprint_id=(
            discovered.get("active_sprint_id") or index_data.get("active_sprint_id")
        ),
        project_manifest=(
            project_rows[0]
            if project_rows and isinstance(project_rows[0], dict)
            else {}
        ),
    )
    existing_findings = {
        (item.get("code"), item.get("slug"), item.get("message")) for item in findings
    }
    findings.extend(
        item
        for item in roadmap["wiring_findings"]
        if (item.get("code"), item.get("slug"), item.get("message"))
        not in existing_findings
    )

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
# Agent-facing MCP surface = read_plan + edit_plan + roadmap + audit + crew. The
# granular _funcs below remain for tests/internal use but are intentionally NOT
# registered (collapsed per the schema-and-tooling plan); full removal is a
# later cleanup. read_plan folds the 5 legacy reads (list_plans/list_projects/
# list_sprints/list_followups/list_questions) via its discovery + with_schema
# modes; edit_plan folds the granular mutators via its set/append/resolve/lock/
# move + create ops; crew folds run state over four views and writes nothing.

if mcp is not None:
    read_plan_tool = mcp.tool()(_read_plan)
    edit_plan_tool = mcp.tool(name="_edit_plan")(_edit_plan_tool)
    roadmap_tool = mcp.tool()(_roadmap)
    audit_tool = mcp.tool()(_audit)
    crew_tool = mcp.tool()(_crew)


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
