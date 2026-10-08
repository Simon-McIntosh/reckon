# ruff: noqa: I001, PLC0414
"""reckon MCP server — stdio entrypoint.

Registers all reckon.* tools and delegates IO to _store.py.

Version-write contract mirrors POST /plan/<project>/<slug> in
~/Code/reckon/reckon/serve.py. Both rewrite the plan semantic HTML state atomically
using the same `version` optimistic-concurrency field.  The docs-server owns
the browser UI; this MCP server owns the agent IO path.  They coexist safely
because both use atomic .tmp rename.

For "index" and "project" slugs, the JSON-envelope backing (_version field)
remains canonical — sprints/milestones live there.

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
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

# ── SDK import ─────────────────────────────────────────────────────────────
try:
    from mcp.server.fastmcp import FastMCP

    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False
    FastMCP = None  # type: ignore[assignment,misc]

import reckon.crew.resumption as resumption_module
from reckon import (
    _plan_html,
)
from reckon import (
    budget as budget_module,
)
from reckon import (
    capabilities as capabilities_module,
)
from reckon import (
    crew as crew_module,
)
from reckon import (
    flight as flight_module,
)
from reckon import (
    ledger as ledger_module,
)
from reckon import (
    velocity as velocity_module,
)
from reckon._schema import (
    PlanState,
    gen_json_schema,
    is_section_identity,
    parse_plan_ref,
    section_depends_on,
    standalone_reason,
)
from reckon._store import (
    _docs_dir_for_project,
    _resolve_html_file,
    _state_root,
    list_followups_across,
    list_questions_across,
    read_plan,
    state_path,
)
from reckon.capability import (
    map_legacy_capabilities,
)
from reckon.crew import obligations as obligations_module
from reckon.crew.directory import DirectoryError
from reckon.crew.directory import directory as crew_directory
from reckon.crew.query import RunQueryError, project_live_rows
from reckon.crew.query import runs_view as crew_runs_view
from reckon.crew.runs import project_watch_visibility
from reckon.doccheck import SEVERITIES, audit_file, audit_lifecycle, audit_links
from reckon.mcp_views import (
    ResourceSelector,
    ViewRequestError,
    audit_view,
    authored_plan_text,
    crew_lanes_view,
    discovery_view,
    error_response,
    normalize_selector,
    normalize_view,
    resource_view,
    roadmap_view,
    storage_schema_for,
)
from reckon.project_state import (
    ProjectStateError,
    audit_project_state,
    legacy_index_path,
    resource_path,
)
from reckon.resources import (
    ResourceCollision,
    canonical_type,
    composed_provenance,
    content_provenance,
    identify_resource,
    resolve_resource,
    resource_map,
)
from reckon.roadmap import GraphTargetError, build_roadmap, resolve_graph_target

# Names moved into sibling modules stay importable from reckon.mcp so that
# `from reckon.mcp import <name>` and attribute access by other modules keep
# resolving; the redundant aliases mark them as deliberate re-exports.
from reckon.mcp_deadlines import (
    DEADLINE_ENV as DEADLINE_ENV,
    LANDING_GRACE_ENV as LANDING_GRACE_ENV,
    LANDING_POLL_SECONDS as LANDING_POLL_SECONDS,
    LANDING_STAT_MARGIN_SECONDS as LANDING_STAT_MARGIN_SECONDS,
    READ_DEADLINE_ENV as READ_DEADLINE_ENV,
    READ_DEADLINE_SECONDS as READ_DEADLINE_SECONDS,
    RUN_ID_ENV as RUN_ID_ENV,
    WRITE_DEADLINE_ENV as WRITE_DEADLINE_ENV,
    WRITE_DEADLINE_SECONDS as WRITE_DEADLINE_SECONDS,
    WRITE_LANDING_GRACE_SECONDS as WRITE_LANDING_GRACE_SECONDS,
    _CLI_ANSWER_COMMANDS as _CLI_ANSWER_COMMANDS,
    _COMPUTING_WORK_FLOOR_SECONDS as _COMPUTING_WORK_FLOOR_SECONDS,
    _COMPUTING_WORK_FRACTION as _COMPUTING_WORK_FRACTION,
    _GUARDED_WRITE_HINT as _GUARDED_WRITE_HINT,
    _INFLIGHT_READS as _INFLIGHT_READS,
    _InflightBody as _InflightBody,
    _UNSET as _UNSET,
    _await_body as _await_body,
    _bounded_landing as _bounded_landing,
    _cause_of_timeout as _cause_of_timeout,
    _conflict_response as _conflict_response,
    _crew_call_kind as _crew_call_kind,
    _deadline_seconds as _deadline_seconds,
    _deadline_tool as _deadline_tool,
    _document_path_hint as _document_path_hint,
    _edit_success_response as _edit_success_response,
    _file_fingerprint as _file_fingerprint,
    _forget_read as _forget_read,
    _landing_grace_seconds as _landing_grace_seconds,
    _landing_state as _landing_state,
    _op_error_response as _op_error_response,
    _plan_path_hint as _plan_path_hint,
    _positive_seconds as _positive_seconds,
    _read_join_key as _read_join_key,
    _read_joining_running as _read_joining_running,
    _resolved_signature as _resolved_signature,
    _resource_reference as _resource_reference,
    _resource_title as _resource_title,
    _review_owed_fields as _review_owed_fields,
    _run_scoped_checkout as _run_scoped_checkout,
    _run_scoped_read_root as _run_scoped_read_root,
    _run_scoped_refusal as _run_scoped_refusal,
    _run_scoped_write_guard as _run_scoped_write_guard,
    _run_under_deadline as _run_under_deadline,
    _run_worktree as _run_worktree,
    _stale_plan_review as _stale_plan_review,
    _start_body as _start_body,
    _thread_cpu_seconds as _thread_cpu_seconds,
    _thread_run_wait_seconds as _thread_run_wait_seconds,
    _written_path as _written_path,
)
from reckon.mcp_discovery import (
    _STANDALONE_SET_PATH as _STANDALONE_SET_PATH,
    _aggregate_version as _aggregate_version,
    _apply_standalone_meta as _apply_standalone_meta,
    _audit_sprint_findings as _audit_sprint_findings,
    _bounded_edit_distance as _bounded_edit_distance,
    _discover_project as _discover_project,
    _discovery_state_root as _discovery_state_root,
    _discovery_summary as _discovery_summary,
    _extract_standalone_declaration as _extract_standalone_declaration,
    _filter_inventory as _filter_inventory,
    _finding as _finding,
    _index_discovery as _index_discovery,
    _index_project_summary as _index_project_summary,
    _inventory_row as _inventory_row,
    _list_plans as _list_plans,
    _matches_search as _matches_search,
    _plan_html_text as _plan_html_text,
    _rollup_counts as _rollup_counts,
    _sprint_item_slug as _sprint_item_slug,
    _summary_sprint_rows as _summary_sprint_rows,
    _tag_audit_findings as _tag_audit_findings,
    _tag_inventory as _tag_inventory,
    _unwired_plan_refusal as _unwired_plan_refusal,
)
from reckon.mcp_edit_plan import (
    _add_research as _add_research,
    _add_sprint_item as _add_sprint_item,
    _append_comment as _append_comment,
    _append_followup as _append_followup,
    _create_sprint as _create_sprint,
    _edit_plan as _edit_plan,
    _edit_plan_prose as _edit_plan_prose,
    _edit_plan_tool as _edit_plan_tool,
    _list_followups as _list_followups,
    _list_projects as _list_projects,
    _list_questions as _list_questions,
    _list_sprints as _list_sprints,
    _lock_decision as _lock_decision,
    _move_sprint_item as _move_sprint_item,
    _patch_plan as _patch_plan,
    _pending_limit_warning as _pending_limit_warning,
    _pending_plan_limit as _pending_plan_limit,
    _pending_plans as _pending_plans,
    _plans_nearest_to_closing as _plans_nearest_to_closing,
    _project_manifest as _project_manifest,
    _resolve_followup as _resolve_followup,
    _resolve_question as _resolve_question,
    _set_impl as _set_impl,
    _set_status as _set_status,
    _update_inventory_item as _update_inventory_item,
    _update_sprint as _update_sprint,
    _validate_working as _validate_working,
)

_LOGGER = logging.getLogger("reckon.mcp")

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
    section: str | None = None,
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
            workflow, ``history`` paginates prior workflow, ``version`` returns
            only typed identity and the concurrency token, ``raw`` returns the
            lossless storage state, ``section`` returns one authored h2 section
            for a plan, or the whole composed text of a cumulative evidence
            record (a ``section`` argument on a record is refused with
            ``section_not_applicable``), and ``schema`` describes the response
            plus the selected resource's storage schema. Full followup
            prompts require ``view="detail", include_prompts=True``.

      read_plan(project)                 [slug omitted/None]
          → DISCOVERY: { project, plans, followups, questions, sprints,
            milestones, active_sprint_id, tag_inventory, summary } — folds
            list_plans / list_followups / list_questions / list_sprints into one call.
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
    # A read made from inside a crew run belongs to that run's own worktree.
    # With no explicit checkout_path, a read of the run's own project resolves
    # there, so the worker reads its base revision rather than the coordinator's
    # live copy. Reads of other projects, and callers with no run, are unchanged.
    if checkout_path is None:
        checkout_path = _run_scoped_read_root(project)

    if resource is not None or view is not None:
        return _read_plan_view(
            project=project,
            slug=slug,
            checkout_path=checkout_path,
            doc_type=doc_type,
            resource=resource,
            view=view,
            section=section,
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
        inventory = [_inventory_row(item) for item in discovered.get("inventory", [])]
        if search:
            inventory = [
                {
                    **item,
                    "body_text": authored_plan_text(
                        _plan_html_text(
                            project,
                            str(item.get("slug") or ""),
                            checkout_path,
                            str(item.get("type") or "plan"),
                        )
                    ),
                }
                if item.get("type") == "plan"
                else item
                for item in inventory
            ]
        plans = _filter_inventory(
            inventory,
            status=status,
            doc_type=doc_type,
            sprint=sprint,
            milestone=milestone,
            owner=owner,
            search=search,
            limit=limit,
        )
        for plan in plans:
            plan.pop("body_text", None)
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
            "tag_inventory": _tag_inventory(inventory),
            "summary": _discovery_summary(
                project,
                plans,
                followups,
                questions,
                sprints=list(discovered.get("sprints") or []),
                all_plans={
                    str(item.get("slug")): item
                    for item in inventory
                    if item.get("slug")
                },
                docs_dir=_docs_dir_for_project(project, checkout_path),
            ),
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
    if data and canonical_type(data.get("type")) == "plan":
        # The standalone declaration is authored markup the state engine
        # leaves untouched, so it is read from its own header: a plan
        # that declares itself standalone round-trips its reason through read.
        reason = standalone_reason(
            _plan_html_text(project, slug, checkout_path, doc_type)
        )
        if reason:
            data["standalone"] = reason

    if slug in ("index", "project") and doc_type is None:
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
    if data and canonical_type(data.get("type")) == "plan":
        # Section-scoped refs resolve through the same read, so a caller sees
        # the waiting section beside the whole-plan dependencies it already had.
        section_rows = _section_dependency_rows(
            _plan_html_text(project, slug, checkout_path, doc_type),
            project,
            checkout_path,
        )
        if section_rows:
            result["deps"] = [*result.get("deps", []), *section_rows]
    if with_schema:
        result["schema"] = gen_json_schema()
        result["dos_donts"] = _DOS_DONTS
        result["op_vocab"] = _OP_VOCAB
    return result


def _read_plan_tool(
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
    section: str | None = None,
    cursor: str | None = None,
    include_prompts: bool = False,
) -> dict[str, Any]:
    """Return a transport-bounded plan or project read by default.

    ``view`` names the response shape for a typed resource read: ``'summary'``
    (the default), ``'detail'``, ``'history'``, ``'version'``, ``'raw'``,
    ``'schema'`` and ``'section'``. For a plan, ``view='section'`` returns one
    authored section selected by its ``section`` argument — the id carried on
    its h2 or on the ``<section>`` element wrapping it — and refuses with
    ``section_not_found`` when that id is absent, listing the available
    section identities. A cumulative
    evidence record is served whole rather than section by section: it returns
    the record's composed text, or the record's own bytes with a warning on the
    response when composition failed, and a ``section`` argument on a record is
    refused with ``section_not_applicable``. Request ``view='raw'`` explicitly
    for the lossless storage response. The explicit legacy schema injector
    remains unchanged for callers using ``with_schema``.
    """

    selected_view = view
    if view is None and not with_schema and project not in (None, "*"):
        selected_view = "summary"
    return _read_plan(
        project=project,
        slug=slug,
        with_schema=with_schema,
        checkout_path=checkout_path,
        status=status,
        doc_type=doc_type,
        sprint=sprint,
        milestone=milestone,
        owner=owner,
        search=search,
        limit=limit,
        include_followups=include_followups,
        include_questions=include_questions,
        resource=resource,
        view=selected_view,
        section=section,
        cursor=cursor,
        include_prompts=include_prompts,
    )


def _read_archived_resource(
    project: str,
    slug: str,
    doc_type: str,
    checkout_path: str | None,
) -> tuple[dict[str, Any], int]:
    """Read exactly one archived typed artifact without live-resource ambiguity."""

    docs_dir = _docs_dir_for_project(project, checkout_path)
    if docs_dir is None:
        return {}, 0
    key = (canonical_type(doc_type), slug, True)
    resource = resource_map(
        docs_dir, project, include_archived=True, ignore_invalid=True
    ).get(key)
    if resource is None:
        return {}, 0
    data = _plan_html.read_state(
        resource.path.read_text(encoding="utf-8", errors="replace")
    )
    return data, int(data.get("version", 0) or 0)


def _typed_resource_for_selector(docs_dir: Path, selector: Any) -> Any:
    """Return the resource a typed selector names, without walking the corpus.

    The provenance of a typed read is the selected document's own path, so the
    read resolves that one document at its canonical location rather than
    building the project's whole resource map to pick one entry out of it. A
    slug the canonical path cannot settle — a live and an archived document
    sharing it — falls back to the map, which owns the duplicate's collision
    rule, so the answer matches the scan for a duplicated slug.
    """

    try:
        resource = resolve_resource(
            docs_dir,
            selector.project,
            selector.id,
            selector.type,
            include_archived=selector.archived,
        )
    except ResourceCollision:
        resource = None
    if resource is None:
        resource = resource_map(
            docs_dir,
            selector.project,
            include_archived=True,
            ignore_invalid=True,
        ).get((selector.type, selector.id, selector.archived))
    return resource


def _typed_resource_provenance(
    selector: Any,
    checkout_path: str | None,
    composed_content: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return provenance for the canonical file selected by a typed read."""

    docs_dir = _docs_dir_for_project(selector.project, checkout_path)
    if docs_dir is None:
        raise FileNotFoundError(f"no docs dir for project {selector.project!r}")
    if selector.type in {"plan", "research", "evidence"}:
        resource = _typed_resource_for_selector(docs_dir, selector)
        if resource is None:
            raise FileNotFoundError(
                f"{selector.type} resource {selector.project}:{selector.id} was not found"
            )
        content_path = resource.path
    else:
        content_path = resource_path(
            docs_dir,
            selector.project,
            selector.type,
            selector.id,
        )
        if not content_path.is_file() and not _distributed(docs_dir):
            # Only a project whose aggregate is still canonical may cite it as
            # provenance. In distributed mode that file is superseded, and
            # naming it as the source of a typed resource attributes live state
            # to a record frozen at migration.
            content_path = legacy_index_path(docs_dir, selector.project)
        if not content_path.is_file() and checkout_path is None:
            content_path = state_path(selector.project, "index")
    if not content_path.is_file() and composed_content is not None:
        return composed_provenance(docs_dir.parent, composed_content)
    return content_provenance(docs_dir.parent, content_path)


def _typed_resource_text(
    selector: ResourceSelector,
    checkout_path: str | None,
) -> str:
    """Read the exact live or archived typed artifact selected by a view.

    A cumulative evidence record reads composed — its own bytes followed by the
    fragments of the plan it documents — so a caller of the text path sees the
    anchors a fragment carries. Any other document, and a record whose ledger
    cannot be read, reads as its own bytes.
    """

    text, _warning = _typed_resource_text_and_warning(selector, checkout_path)
    return text


def _typed_resource_text_and_warning(
    selector: ResourceSelector,
    checkout_path: str | None,
) -> tuple[str, str | None]:
    """Read the selected artifact, naming a composition fallback it took.

    Returns the text and, when a record could not be composed and its own bytes
    were read instead, a warning naming the record and the failure. The warning
    is also logged, so an uncomposed read is observable rather than silent: a
    record whose fragments are hidden by a read failure otherwise reads exactly
    like one that has none. ``None`` means the read is the healthy one.
    """

    from reckon.evidence import (
        EvidenceSynthesisError,
        compose_landed_record,
        evidence_record_plan,
    )

    docs_dir = _docs_dir_for_project(selector.project, checkout_path)
    if docs_dir is None or selector.type not in {"plan", "research", "evidence"}:
        return "", None
    resource = resource_map(
        docs_dir,
        selector.project,
        include_archived=True,
        ignore_invalid=True,
    ).get((selector.type, selector.id, selector.archived))
    if resource is None:
        return "", None
    plan = evidence_record_plan(resource.path)
    if plan is not None:
        try:
            composed = compose_landed_record(
                resource.path, plan, project=selector.project
            ).decode("utf-8", errors="replace")
        except (OSError, EvidenceSynthesisError) as exc:
            # An unreadable ledger suppresses composition, not the record's own
            # bytes — but the gap must be visible, so it is logged and returned.
            warning = f"evidence record {resource.path.name} read uncomposed: {exc}"
            _LOGGER.warning("%s", warning)
            return _read_resource_bytes(resource.path), warning
        return composed, None
    return _read_resource_bytes(resource.path), None


def _read_resource_bytes(path: Path) -> str:
    """Return a document's own text, or ``""`` when it cannot be read."""

    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _append_read_warning(result: dict[str, Any], warning: str | None) -> dict[str, Any]:
    """Attach a read fallback warning to a response's ``warnings`` list."""

    if warning:
        result["warnings"] = [*(result.get("warnings") or []), warning]
    return result


def _distributed(docs_dir: Path) -> bool:
    """Report whether a docs tree's typed resources are the canonical store."""
    from reckon.project_state import ProjectStateError, project_state_mode

    try:
        return project_state_mode(docs_dir).format == "distributed"
    except ProjectStateError:
        return False


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


def _require_section_resource(
    selected_view: str,
    resource: dict[str, Any] | None,
    slug: str | None,
) -> None:
    """Refuse a section view that has no single plan identity to select from."""

    if selected_view == "section" and resource is None and slug is None:
        raise ViewRequestError(
            "section_resource_required",
            "view='section' requires one selected plan resource.",
            "Pass project and slug, or a typed plan resource selector.",
        )


def _read_plan_view(
    *,
    project: str | None,
    slug: str | None,
    checkout_path: str | None,
    doc_type: str | None,
    resource: dict[str, Any] | None,
    view: str | None,
    section: str | None,
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

    # The run scope reaches a typed read too: a resource may name its project
    # without a top-level project argument, so the read resolves the run's own
    # worktree from whichever project the call names.
    if checkout_path is None:
        reading = project
        if reading is None and isinstance(resource, Mapping):
            reading = resource.get("project")
        checkout_path = _run_scoped_read_root(
            reading if isinstance(reading, str) else None
        )

    selector: ResourceSelector | None = None
    try:
        selected_view = normalize_view(view)
        _require_section_resource(selected_view, resource, slug)
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
            # The summary is the list-level read: it answers from the index.
            # A search reads the documents' bodies, and the other views ask
            # for derived state, so those keep the derived path.
            raw = (
                _index_project_summary(
                    project,
                    checkout_path,
                    status=status,
                    doc_type=doc_type,
                    sprint=sprint,
                    milestone=milestone,
                    owner=owner,
                    limit=None,
                )
                if selected_view == "summary" and search is None
                else _read_plan(
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
            )
            discovery_selector = ResourceSelector(
                project=project,
                type="project",
                id="project",
            )
            return discovery_view(
                project,
                raw,
                view=selected_view,
                provenance=_typed_resource_provenance(
                    discovery_selector, checkout_path, raw
                ),
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

        if selector.type == "project" and selected_view not in {"raw", "version"}:
            raw_discovery = (
                _index_project_summary(selector.project, checkout_path, limit=None)
                if selected_view == "summary"
                else _read_plan(
                    project=selector.project,
                    checkout_path=checkout_path,
                    limit=None,
                    include_followups=include_followups,
                    include_questions=include_questions,
                )
            )
            result = discovery_view(
                selector.project,
                raw_discovery,
                view=selected_view,
                provenance=_typed_resource_provenance(
                    selector, checkout_path, raw_discovery
                ),
                cursor=cursor,
                limit=limit,
                include_prompts=include_prompts,
                storage_schema=storage_schema_for("project"),
                op_vocab=_OP_VOCAB,
                dos_donts=_DOS_DONTS,
            )
            result["resource"] = selector.as_dict()
            return result

        # A sprint summary derives its own composition — the sprint list from the
        # project's state, hydrated against the list-level rows — rather than
        # every document in the project, so it is answered before the legacy
        # resource read that would scan the tree to find one sprint. The detail
        # view asks for per-document derived state and keeps the derived path.
        composed_sprint: dict[str, Any] | None = None
        if (
            selector.type == "sprint"
            and selected_view == "summary"
            and not selector.archived
        ):
            discovered = _index_discovery(selector.project, checkout_path)
            composed_sprint = next(
                (
                    item
                    for item in discovered.get("sprints", [])
                    if isinstance(item, dict) and item.get("id") == selector.id
                ),
                None,
            )

        if composed_sprint is not None:
            data = composed_sprint
            version = _aggregate_version(selector.project, checkout_path)
            deps: list[dict[str, Any]] = []
        elif selector.archived:
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
                    in {
                        "sprint",
                        "milestone",
                        "blocker",
                        "timeline",
                        "project",
                        "review",
                    }
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

        if selector.type == "sprint" and selected_view == "detail":
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
            discovered = _index_discovery(selector.project, checkout_path)
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
                held_blockers = [
                    item.get("id")
                    for item in inventory_plan.get("blocking", [])
                    if isinstance(item, dict)
                    and item.get("kind") == "held"
                    and item.get("id")
                ]
                if explicit_blockers:
                    data = {**data, "blocked_by": explicit_blockers}
                if held_blockers:
                    data = {**data, "held_by": held_blockers}

        html_text = None
        html_warning = None
        if selected_view == "section":
            html_text, html_warning = _typed_resource_text_and_warning(
                selector, checkout_path
            )
        result = resource_view(
            selector,
            version,
            data,
            view=selected_view,
            provenance=_typed_resource_provenance(selector, checkout_path),
            deps=deps,
            cursor=cursor,
            limit=limit,
            include_prompts=include_prompts,
            section=section,
            html_text=html_text,
            storage_schema=storage_schema_for(selector.type),
            op_vocab=_OP_VOCAB,
            dos_donts=_DOS_DONTS,
        )
        return _append_read_warning(result, html_warning)
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


def _section_dependency_rows(
    html_text: str,
    owning_project: str,
    checkout_path: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve a plan's section-scoped refs, naming the section that waits.

    The mapping is authored markup the state engine leaves untouched, so it is
    read from the header the same way the standalone declaration is read.
    Each row carries the whole-plan row shape plus ``source_section``, so a
    caller can see which section of the owning plan holds the ref beside the
    target plan and section the ref resolves to.
    """

    rows: list[dict[str, Any]] = []
    for raw_section, raw_refs in (section_depends_on(html_text) or {}).items():
        waiting = str(raw_section or "").strip()
        if not is_section_identity(waiting):
            continue
        refs = [raw_refs] if isinstance(raw_refs, str) else raw_refs
        if not isinstance(refs, list):
            continue
        for ref in refs:
            row = _resolve_plan_ref(str(ref), owning_project, checkout_path)
            row["source_section"] = waiting
            rows.append(row)
    return rows


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
        "give every followup one /reckon-build invocation line; store guidance in the plan.",
        "slug='index' is a composed compatibility read; edit named project resources with doc_type.",
        "use canonical artifact types plan, research, or evidence; doc reads as research.",
        "use project:slug or project:slug#stage provenance refs; unqualified same-project refs remain valid.",
        "depends_on/blocks take the same grammar: bare slug = local, project:slug = external; read_plan(project, slug) resolves them in its deps list.",
        "use depends_on only for executable prerequisites; research, evidence, and specifications use informs.",
        "landed/outcome records carry evidence_for naming the plan(s) whose execution they record — the plan-to-generated-evidence back-link; informs is reserved for INPUTS that feed future work.",
        "use roadmap for pending work, completion, ready/blocked sets, sprint order, critical paths, and wiring findings.",
        "use edit_plan mode='text' for exact version-safe authored HTML replacements — one old_html/new_html pair, or a replacements list applied in order as one versioned write; use mode='state' for structured ops.",
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
    "set": "{op:'set', path:'<dotted>', value:<any>} — artifact scalars, decisions.<key>.<field>, followups.<id>.prompt; one top-level field on a sprint/milestone/blocker/project resource; review scalars and the priority list. impl clamps to 0..1, plan-only.",
    "append": "{op:'append', target:'<collection>', item:<obj|str>[, section][, key]} — plan followups/research/questions/comments/decisions; sprint items; timeline events; review findings. target 'sections' writes a section (item: id, title, body) or attaches a record to an <h2 id> already in the file (item: id, no body); effort_hours, capability and links are required on either shape. followup prompt is one /reckon-build line.",
    "resolve": "{op:'resolve', target:'followups'|'questions'|'findings', id, by, outcome|resolution} — sets resolved_at/by + outcome/resolution; finding status derives from resolved_at.",
    "lock": "{op:'lock', key, choice, rationale, by} — merges the lock into decisions[key], preserving authored title/context/choices.",
    "accept": "{op:'accept', key, by} — sets decisions[key].choice from its stored recommendation (recommended/recommended_by) in one action, recording when and by whom; refused when the decision carries no recommendation.",
    "gate": "{op:'gate', id, section, gated_sections:[...], measure, required_evidence} — declares one open evidence gate.",
    "pass": "{op:'pass', id, evidence} — closes a declared gate as passed; evidence is required when gates.require_evidence is enabled.",
    "fail": "{op:'fail', id, evidence} — closes a declared gate as failed while preserving negative evidence.",
    "retire_prose": "{op:'retire_prose', preimage:'<exact authored HTML>'} — removes one authored fragment outside every section[data-reckon], atomically with the batch's structured ops.",
    "insert_section": "{op:'insert_section', id, title, body, effort_hours, capability, links} — writes a new h2 with its typed section record; effort_hours, capability and links are required.",
    "collapse_section": "{op:'collapse_section', section, summary, evidence_anchor} — replaces the authored body under the section's heading with the landed card (✓ landed badge, summary, evidence link), keeps the heading and its id, and sets the section's declaration to done. A bare evidence_anchor names a section of this plan's landing record, appended in the same batch or already there, and links as /<project>/evidence/archive/<plan>-landed.html#<anchor>; one the record lacks is refused. An anchor with a path is kept as written.",
    "append_evidence": "{op:'append_evidence', plan, anchor, title, body} — appends one <section id=anchor> to that plan's cumulative landing record docs/evidence/archive/<plan>-landed.html, creating the record when absent and refusing an anchor that already exists.",
    "move": "{op:'move', target:'sprint_item', slug, to, to_version} — selected source sprint; checks both versions, preserves item metadata.",
    "push": "{op:'push'} — marks the selected sprint active (pushed) in one versioned write and leaves every other sprint's status untouched.",
    "create": "edit_plan(..., expected_version=0, create=True) on a NEW slug → creates a plan or named project resource by doc_type. A plan created at or beyond the project's declared pending-plan limit succeeds and its response carries a warning naming the limit, the pending count and the three pending plans nearest to closing.",
}


def _roadmap(
    project: str,
    checkout_path: str | None = None,
    sprint: str | None = None,
    max_paths: int = 5,
    view: Literal["summary", "detail", "raw"] | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Scan plan dependencies and return executable work plus graph health.

    Portfolio scans default to ``view='summary'`` with per-project completion,
    ready/blocked/deferred counts, and finding totals. ``view='detail'`` adds a
    cursor-paginated findings page; ``view='raw'`` returns the lossless report.
    A ``graph:<handle>`` project target returns the derived cross-project
    dependency closure carried by its endpoint plan. Direct internal calls
    without ``view`` preserve the legacy raw response; the registered MCP
    wrapper defaults single-project clients to the compact summary.
    ``checkout_path`` is accepted for a single project and follows the same
    worktree-routing contract as ``read_plan``.
    """

    if max_paths < 1 or max_paths > 50:
        return {
            "ok": False,
            "error": "invalid_max_paths",
            "detail": "max_paths must be between 1 and 50",
        }
    if project.startswith("graph:"):
        handle = project.removeprefix("graph:").strip()
        if checkout_path is not None:
            return {
                "ok": False,
                "error": "graph_checkout_path_unsupported",
                "handle": handle,
                "detail": "graph targets resolve across registered project mounts",
            }
        try:
            mounted: dict[str, dict[str, Any]] = {}
            for row in _list_projects().get("projects", []):
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                mounted_project = str(row["name"])
                discovered = _discover_project(mounted_project)
                index_data, _version = read_plan(mounted_project, "index")
                project_rows = index_data.get("projects") or []
                manifest = (
                    project_rows[0]
                    if project_rows and isinstance(project_rows[0], dict)
                    else {}
                )
                inventory = [
                    _inventory_row(item) for item in discovered.get("inventory", [])
                ]
                followups_by_plan: dict[str, list[dict[str, Any]]] = {}
                for followup in list_followups_across(
                    mounted_project,
                    unresolved_only=False,
                ):
                    plan_slug = str(followup.get("plan_slug") or "")
                    if plan_slug:
                        followups_by_plan.setdefault(plan_slug, []).append(followup)
                for item in inventory:
                    if item.get("type", "plan") == "plan":
                        item["followups"] = followups_by_plan.get(
                            str(item.get("slug")), []
                        )
                mounted[mounted_project] = {
                    "inventory": inventory,
                    "sprints": list(discovered.get("sprints", [])),
                    "active_sprint_id": (
                        discovered.get("active_sprint_id")
                        or index_data.get("active_sprint_id")
                    ),
                    "project_manifest": manifest,
                    "docs_dir": _docs_dir_for_project(mounted_project),
                }
            return resolve_graph_target(handle, mounted)
        except GraphTargetError as exc:
            return {
                "ok": False,
                "error": "graph_target_unavailable",
                "handle": handle,
                "detail": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 — MCP errors stay structured
            return {
                "ok": False,
                "error": "roadmap_error",
                "project": project,
                "detail": str(exc),
            }
    if project == "*":
        if checkout_path is not None:
            return {
                "ok": False,
                "error": "portfolio_checkout_path_unsupported",
                "detail": "select one project when using checkout_path",
            }
        listed = _list_projects()
        reports = []
        for item in listed.get("projects", []):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            report = _roadmap(
                str(item["name"]),
                sprint=sprint,
                max_paths=max_paths,
                view="raw",
            )
            reports.append(report.get("data", report))
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
        raw = {
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
        selected_view = view or "summary"
        try:
            return roadmap_view(
                raw,
                view=selected_view,
                cursor=cursor,
                limit=limit,
            )
        except ViewRequestError as exc:
            return error_response(exc.code, exc.message, hint=exc.hint)

    try:
        discovered = _discover_project(project, checkout_path)
        index_data, _version = read_plan(project, "index", checkout_path)
        project_rows = index_data.get("projects") or []
        project_manifest = (
            project_rows[0]
            if project_rows and isinstance(project_rows[0], dict)
            else {}
        )
        inventory = [_inventory_row(item) for item in discovered.get("inventory", [])]
        followups_by_plan: dict[str, list[dict[str, Any]]] = {}
        for followup in list_followups_across(
            project,
            unresolved_only=False,
            root=checkout_path,
        ):
            plan_slug = str(followup.get("plan_slug") or "")
            if plan_slug:
                followups_by_plan.setdefault(plan_slug, []).append(followup)
        for item in inventory:
            if item.get("type", "plan") == "plan":
                item["followups"] = followups_by_plan.get(str(item.get("slug")), [])
        raw = build_roadmap(
            project,
            inventory,
            list(discovered.get("sprints", [])),
            active_sprint_id=(
                discovered.get("active_sprint_id") or index_data.get("active_sprint_id")
            ),
            sprint_id=sprint,
            max_paths=max_paths,
            project_manifest=project_manifest,
            docs_dir=_docs_dir_for_project(project, checkout_path),
        )
        if view is None:
            return raw
        try:
            return roadmap_view(raw, view=view, cursor=cursor, limit=limit)
        except ViewRequestError as exc:
            return error_response(exc.code, exc.message, hint=exc.hint)
    except Exception as exc:  # noqa: BLE001 — MCP errors stay structured
        return {
            "ok": False,
            "error": "roadmap_error",
            "project": project,
            "detail": str(exc),
        }


def _roadmap_tool(
    project: str,
    checkout_path: str | None = None,
    sprint: str | None = None,
    max_paths: int = 5,
    view: Literal["summary", "detail", "raw"] | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a transport-bounded roadmap by default for one project.

    Lossless single-project reports routinely exceed the MCP response ceiling,
    so clients request ``view='raw'`` explicitly. Portfolio and graph targets
    retain their existing defaults.
    """

    selected_view = view
    if view is None and project != "*" and not project.startswith("graph:"):
        selected_view = "summary"
    if checkout_path is None:
        checkout_path = _run_scoped_read_root(project)
    return _roadmap(
        project,
        checkout_path=checkout_path,
        sprint=sprint,
        max_paths=max_paths,
        view=selected_view,
        cursor=cursor,
        limit=limit,
    )


def _crew(
    project: str | None = None,
    view: str = "summary",
    checkout_path: str | None = None,
    plan: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    candidates: list[dict[str, Any]] | None = None,
    session: str | None = None,
    action: Literal["resume", "session", "resume-ready", "sweep"] | None = None,
    run_id: str | None = None,
    advice: str | None = None,
    dry_run: bool = False,
    node: str | None = None,
    section: str | None = None,
    member: str | None = None,
    classification: str | None = None,
    resumable: bool | None = None,
    newest_per_node: bool = False,
    source: str = "all",
    scope: str = "project",
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Read crew state or perform one recovery action through the crew surface.

    Deliberately one tool over read views and recovery actions
    rather than a tool per operation. Set ``action`` to ``resume``, ``session``
    or ``resume-ready`` to reach the same implementation as the corresponding
    crew CLI operation. Omit ``action`` for the read views below.
    ``directory`` reads every live coordinator across the workstation, or one
    project's coordinators, and resolves a run or human node id to its owner.
    ``ledger``, ``records`` and ``summary`` read the project's committed runs —
    ``<repo>/docs/state/<project>/crew.json``, the durable half; ``scores``
    reads the per-dimension review scores those rows carry, filtered by
    ``plan``, ``node`` or ``run_id`` so a score is readable against the job that
    earned it, omitting a row with no stored review rather than reporting it as
    zeros and returning a stored review that never parsed with its status and
    no scores; ``live`` reads the never-committed pointers of runs still in
    flight, each carrying the
    classification :func:`reckon.crew.recovery.recover` would give it; ``drain`` derives
    the session-closure count, declared executable remainder, and recorded
    dispositions from those pointers;
    ``scopes`` reads live path claims and partitions the optional ordered
    ``candidates`` wave manifest into mutually independent serial lanes,
    reporting under ``candidate_wave`` whether such a wave was supplied — so
    an empty conflict list is not read as an evaluated wave with no conflicts
    when no wave was asked about — and marking each claim with the binding
    verdict the dispatch scope check itself would apply to that path;
    ``runs`` joins compact, filterable rows from live pointers and the ledger;
    its default project scope reads one repository, while workstation scope
    labels rows from every configured project with their owning repository;
    ``routing`` derives configuration cost and durability across every mounted
    ledger; ``fleet`` reads compact rows for every mounted project's cross-project rollup;
    ``flight`` reports the resolved routing config with the layer that supplied every value; and
    ``budget`` reports, per backend, whether a wave may open — read from what
    earlier runs recorded, so it spends nothing, and holding only where
    exhaustion was actually reported.
    The plan-review view needs project and plan, and returns the newest stored
    record, its unanswered finding ids, and delivered reports.
    ``lanes`` reports which configured endpoints will currently serve a dispatch
    and how much of each five-hour and weekly quota window remains. Consult it
    before choosing a lane; it reports availability only and never selects,
    ranks, or recommends one.
    Do not dispatch background work to an orchestrator lane: it runs the
    orchestrators; background work there costs orchestrator capacity, and
    saturating it stops every session rather than one node.

    ``obligations`` derives the duties one coordinator session still owes — each
    with its kind, run, age and next command — from the live pointers, the
    review store and the ledger, and needs ``session``.
    ``velocity`` reports what the fleet delivered over the window ``since`` to
    ``until`` — promotions split implementation against review, landed lines in
    six classes, the seven-day deletion share, dispatch-to-completion and
    dispatch-to-promotion quantiles, attempts per landed node and the review
    share — by project, by lane, by day and in project-lane-day cells. It takes
    one ``project``, or ``"*"`` for every mounted checkout, and refuses by name
    when ``since`` is absent; ``until`` defaults to now. The answer is the
    per-project, per-lane and per-day tables plus the count of the
    project-lane-day cells. Every other block is served only when the caller
    names it in ``fields``, and the cells are paged by ``limit`` and ``cursor``
    like every other crew view's records; an unrecognised field is refused with
    the accepted set named.

    Pass ``session`` — the same id given to ``reckon crew dispatch`` — on
    ``live``: every run row gains ``mine``, and the watcher block reports
    ``session_attached`` plus the session-scoped ``attach_line``. Without it the
    answer is project-wide, which says a producer exists and says nothing about
    whether this session will hear its own runs finish. On ``drain``, it counts
    only that session toward closure and reports peer rows separately. On
    ``runs``, ``session`` filters the worker session identity carried by each
    compact row.

    Pass ``fields`` to read only what you asked for: a ``live`` row is narrowed
    to the requested fields plus ``run_id``, and ``runs`` accepts the three
    fields a coordinator checks first — ``log_age_seconds``,
    ``commits_beyond_base`` and ``manifest_reported_status`` — drawn from the
    same classification ``live`` computes. An unknown field is refused with the
    accepted set named.

    ``checkout_path`` follows the same worktree-routing contract as
    ``read_plan``: with it, the ledger and the routing project layer resolve
    inside that checkout instead of the registered main one.
    """
    if action is not None:
        return _crew_recover(
            action,
            project=project,
            run_id=run_id,
            advice=advice,
            dry_run=dry_run,
        )
    if view == "fleet":
        from reckon import fleet_index
        from reckon.serve import load_mounts

        try:
            return {
                "ok": True,
                "view": view,
                "projects": fleet_index.collect_project_rows(
                    load_mounts(), state_root=_state_root()
                ),
            }
        except (OSError, ProjectStateError, ValueError) as exc:
            return {
                "ok": False,
                "error": "crew_error",
                "view": view,
                "detail": str(exc),
            }
    if view == "directory":
        try:
            return crew_directory(project, run_id=run_id, node_id=node)
        except DirectoryError as exc:
            return {
                "ok": False,
                "error": "directory_error",
                "project": project,
                "detail": str(exc),
            }
    if not project:
        return {
            "ok": False,
            "error": "missing_project",
            "detail": "crew read views need project",
        }
    if view not in (
        "summary",
        "flight",
        "live",
        "scopes",
        "drain",
        "records",
        "ledger",
        "budget",
        "lanes",
        "routing",
        "directory",
        "fleet",
        "runs",
        "obligations",
        "velocity",
        "scores",
        "plan-review",
    ):
        return {
            "ok": False,
            "error": "invalid_view",
            "detail": (
                "view must be directory, drain, scopes, summary, flight, live, "
                "records, ledger, budget or obligations; lanes is the endpoint "
                "quota view, routing is the cross-ledger cost view, runs is the "
                "compact joined view, scores is the per-dimension review view, "
                "velocity is the delivery-rate view, and fleet is the "
                "cross-project view; plan-review reads a plan review and its unanswered findings"
            ),
        }
    if view == "plan-review":
        from reckon.crew import plan_review

        if since is not None:
            # With a window, the view folds the committed records into the
            # per-plan day summary; ``plan`` narrows that fold, so it is no
            # longer required and the flat read below keeps it so.
            try:
                summary = plan_review.review_day_summary(
                    project,
                    since=since,
                    until=until if until is not None else datetime.now(UTC).isoformat(),
                    plan=plan,
                )
            except (OSError, ValueError) as exc:
                return {
                    "ok": False,
                    "error": "crew_error",
                    "view": view,
                    "detail": str(exc),
                }
            return {"day_summary": summary}

        if not plan:
            return {
                "ok": False,
                "error": "missing_plan",
                "detail": "plan-review needs project and plan",
            }
        try:
            plan_path = _resolve_html_file(project, plan, artifact_type="plan")
            record = plan_review.read_plan_review(
                project, plan, plan=plan_path if plan_path is not None else ""
            )
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "error": "crew_error",
                "view": view,
                "detail": str(exc),
            }
        payload = {
            "record": record,
            "unanswered": plan_review.unanswered_findings(record) if record else [],
            "delivered": plan_review.delivered_reports(project, plan),
        }
        if record is None:
            stale = _stale_plan_review(project, plan)
            if stale is not None:
                payload["stale_review"] = {
                    "plan_version": stale[0],
                    "detail": stale[1],
                }
        return payload
    try:
        if view == "obligations":
            if not session:
                return {
                    "ok": False,
                    "error": "missing_session",
                    "project": project,
                    "detail": (
                        "obligations are derived for one coordinator session; "
                        "pass the session id given to crew dispatch"
                    ),
                }
            return {
                "ok": True,
                "view": view,
                **obligations_module.obligations(project, session),
            }
        if view == "velocity":
            # The window parsing, the mount resolution, the default summary and
            # the paging of the cells live in one function, so this read and the
            # command line's serve the same payload for the same window.
            return velocity_module.view(
                project,
                since=since,
                until=until,
                checkout_path=checkout_path,
                fields=fields,
                limit=limit,
                cursor=cursor,
            )
        if view == "runs":
            return crew_runs_view(
                project,
                run_id=run_id,
                checkout_path=checkout_path,
                source=source,
                scope=scope,
                node=node,
                plan=plan,
                section=section,
                session=session,
                member=member,
                classification=classification,
                resumable=resumable,
                newest_per_node=newest_per_node,
                fields=fields,
                limit=limit,
            )
        if view == "scopes":
            docs_dir = _docs_dir_for_project(project, checkout_path)
            if docs_dir is None:
                raise crew_module.CrewError(
                    f"project {project!r} has no readable docs directory"
                )
            repo_root = (
                Path(checkout_path).expanduser().resolve()
                if checkout_path is not None
                else docs_dir.parent
            )
            index_data, _version = read_plan(project, "index", checkout_path)
            projects = index_data.get("projects") or []
            manifest = projects[0] if projects and isinstance(projects[0], dict) else {}
            planned = crew_module.plan_scope_lanes(
                candidates or [],
                project=project,
                repo=repo_root,
                derivations=manifest.get("derivations") or {},
            )
            claim_map: dict[str, list[dict[str, Any]]] = {}
            for claim in planned["claims"]:
                # Every owner carries a verdict. A claim that arrives without
                # one is treated as binding, the same answer the read model
                # defaults to and the rule gives a pointer whose worker is not
                # yet recorded; a reader must never have to guess the absence.
                owner: dict[str, Any] = {
                    "run_id": claim["run_id"],
                    "node": claim["node"],
                    "declared_path": claim["declared_path"],
                    "binding": bool(claim.get("binding", True)),
                    "disposition_reason": str(claim.get("disposition_reason", "")),
                }
                if claim.get("derived_from") is not None:
                    owner["derived_from"] = claim["derived_from"]
                claim_map.setdefault(claim["path"], []).append(owner)
            return {
                "ok": True,
                "project": project,
                "view": view,
                "repository": str(repo_root),
                "claim_map": claim_map,
                **planned,
            }
        if view == "budget":
            config = flight_module.resolve(project, checkout_path=checkout_path).config
            return {
                "ok": True,
                "view": view,
                **budget_module.preflight(
                    project,
                    config,
                    root=checkout_path,
                    windows=budget_module.recorded_windows(
                        project, config, root=checkout_path
                    ),
                    document_path=budget_module.published_document_path(),
                    ready=candidates or [],
                ),
            }
        if view == "lanes":
            config = flight_module.resolve(project, checkout_path=checkout_path).config
            mounted = flight_module.mounted_project_docs()
            ledger_sources: dict[str, str | None] = {
                mounted_project: str(docs_dir.parent)
                for mounted_project, docs_dir in mounted.items()
            }
            ledger_sources[project] = checkout_path or ledger_sources.get(project)
            runs = [
                record
                for mounted_project, repository in ledger_sources.items()
                for record in ledger_module.runs(mounted_project, repository)
            ]
            runs.extend(crew_module.list_live())
            return {
                "ok": True,
                "project": project,
                "view": view,
                **crew_lanes_view(config, runs),
            }
        if view == "routing":
            try:
                routing = capabilities_module.routing_surface(
                    project, checkout_path=checkout_path
                )
            except (OSError, ValueError) as exc:
                return {
                    "ok": False,
                    "error": "crew_error",
                    "project": project,
                    "detail": str(exc),
                }
            return {
                "ok": True,
                "project": project,
                "view": view,
                **routing,
            }
        if view == "flight":
            return {
                "ok": True,
                "project": project,
                "view": view,
                **flight_module.flight_report(project, checkout_path=checkout_path),
            }
        if view == "live":
            live_records = [
                record
                for record in crew_module.list_live()
                if str(record.get("project") or "") == project
            ]
            # `session` is what turns a project-wide read into this session's
            # read: it marks which rows are the caller's own where several
            # coordinators share a project, and it is the only way the watcher
            # block can answer whether this caller will be told anything.
            # `fields` narrows each row to what was asked for, so a caller
            # reading one fact no longer receives the whole classification.
            runs = project_live_rows(live_records, fields=fields, session=session)
            return {
                "ok": True,
                "project": project,
                "view": view,
                "session": session,
                "watcher": project_watch_visibility(project, session=session),
                "runs": runs,
            }
        if view == "drain":
            return {
                "ok": True,
                "view": view,
                **crew_module.drain(project, session=session),
            }
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
        if view == "records":
            runs, version = ledger_module.read_records(
                project,
                checkout_path,
                plan=plan,
                since=since,
                limit=limit,
            )
            return {
                "ok": True,
                "project": project,
                "view": view,
                "version": version,
                "path": str(ledger_module.ledger_path(project, checkout_path)),
                "runs": runs,
            }
        if view == "scores":
            # The committed rows are the only input: the reader is
            # :func:`reckon.ledger.review_scores`, so a row whose review was
            # never stored is left out here rather than turned into zeros.
            runs, version = ledger_module.read_records(
                project,
                checkout_path,
                plan=plan,
                since=since,
                limit=limit,
            )
            return {
                "ok": True,
                "project": project,
                "view": view,
                "version": version,
                "path": str(ledger_module.ledger_path(project, checkout_path)),
                "scores": ledger_module.review_scores(
                    runs,
                    plan=plan,
                    node=node,
                    run_id=run_id,
                ),
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
        RunQueryError,
    ) as exc:
        return {
            "ok": False,
            "error": "crew_error",
            "project": project,
            "detail": str(exc),
        }


def _crew_recover(
    action: str,
    project: str | None = None,
    run_id: str | None = None,
    advice: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Recover a run a provider refusal stopped, through the CLI's own functions.

    Three actions, so a coordinator meeting a refusal never has to shell out:

    ``resume`` answers a stuck worker with ``advice`` in its own session,
    exactly as ``crew resume`` does — same :func:`resume_plan`, same spawn,
    same resumption record — and reports the session it reattached to plus the
    source that supplied it (pointer, stream, or the promoted ledger row). A
    run whose process is still alive is refused with the same message
    ``crew resume`` gives; resuming a live run is the error that needs a stop
    first.

    ``session`` answers the same question ``crew observe`` now answers for
    every run: the session id and its source, or — never a bare null — a
    stated absence naming every source that was consulted. It calls the same
    :func:`reckon.crew.resumption.resolve_session` the CLI calls, so the two
    surfaces cannot disagree about one run.

    ``resume-ready`` is the same action as ``crew resume-ready``: it resumes
    every run whose provider hold or declared external wait has ended, one
    resume per run, and ``dry_run`` reports what would be resumed without
    resuming anything.

    Every refusal here is a readable dict carrying its reason, never a raised
    exception — a coordinator reading a stack trace mid-outage is the state
    this surface exists to prevent.
    """
    if action not in ("resume", "session", "resume-ready", "sweep"):
        return {
            "ok": False,
            "error": "invalid_action",
            "detail": "action must be resume, session, or resume-ready",
        }
    if action in ("resume-ready", "sweep"):
        if not project:
            return {
                "ok": False,
                "error": "missing_project",
                "detail": "resume-ready needs project",
            }
        try:
            report = resumption_module.sweep(project, dry_run=dry_run)
        except crew_module.CrewError as exc:
            return {
                "ok": False,
                "error": "crew_error",
                "project": project,
                "detail": str(exc),
            }
        return {"ok": True, "action": "resume-ready", **report}

    if not run_id:
        return {
            "ok": False,
            "error": "missing_run_id",
            "detail": f"{action} needs run_id",
        }

    if action == "session":
        return {
            "ok": True,
            "action": action,
            **resumption_module.resolve_session(run_id),
        }

    # action == "resume"
    if not advice:
        return {
            "ok": False,
            "error": "missing_advice",
            "detail": "resume needs advice",
        }
    try:
        record = crew_module.read_pointer(run_id)
    except crew_module.CrewError as exc:
        return {
            "ok": False,
            "error": "crew_error",
            "run_id": run_id,
            "detail": str(exc),
        }
    resolved = resumption_module.resolve_session(run_id, record=record)
    project_name = str(record.get("project") or "")
    config = None
    if project_name:
        try:
            config = flight_module.resolve(
                project_name, checkout_path=record.get("repo")
            ).config
        except flight_module.FlightConfigError as exc:
            return {
                "ok": False,
                "error": "flight_error",
                "run_id": run_id,
                "detail": str(exc),
            }
    from reckon.crew.dispatch import LanePaused, _require_fleet_gate_open

    try:
        plan = crew_module.resume_plan(run_id, advice, config=config)
    except LanePaused as exc:
        return {
            "ok": False,
            "error": "lane-paused",
            "run_id": run_id,
            "detail": str(exc),
            "reason": exc.gate.get("reason"),
            "lane_gate": exc.gate,
        }
    except crew_module.BudgetHold as exc:
        return {
            "ok": False,
            "error": "budget-hold",
            "run_id": run_id,
            "detail": str(exc),
            "hold": exc.verdict,
        }
    except crew_module.CrewError as exc:
        return {
            "ok": False,
            "error": "crew_error",
            "run_id": run_id,
            "detail": str(exc),
        }
    directory = crew_module.run_dir(run_id)
    turn = len(list(directory.glob("resume-*.jsonl"))) + 1
    advice_path = directory / f"resume-{turn}-advice.txt"
    resumption_module._write_resume_prompt(advice_path, plan=plan, advice=advice)
    log_path = directory / f"resume-{turn}.jsonl"
    stderr_path = directory / f"resume-{turn}.stderr.log"
    current = crew_module.read_pointer(run_id)
    manifest_baseline_mtime_ns = crew_module._manifest_mtime_ns(
        current.get("manifest_path") or ""
    )
    attempt_started_at = crew_module._utc_now()
    try:
        _require_fleet_gate_open()
        pid = crew_module._spawn(
            plan, log_path=log_path, stderr_path=stderr_path, prompt_path=advice_path
        )
    except LanePaused as exc:
        return {
            "ok": False,
            "error": "lane-paused",
            "run_id": run_id,
            "detail": str(exc),
            "reason": exc.gate.get("reason"),
            "lane_gate": exc.gate,
        }
    crew_module.record_resumption(
        run_id,
        pid=pid,
        turn=turn,
        log_path=log_path,
        stderr_path=stderr_path,
        attempt_started_at=attempt_started_at,
        manifest_baseline_mtime_ns=manifest_baseline_mtime_ns,
    )
    return {
        "ok": True,
        "action": action,
        "run_id": run_id,
        "pid": pid,
        "log_path": str(log_path),
        "resumed_turn": turn,
        "session_id": resolved["session_id"],
        "session_source": resolved["source"],
        **plan.as_dict(),
    }


# ── audit — plan-schema conformance audit (warn half; never mutates) ────────


def _audit_document(
    path: str,
    *,
    project: str | None,
    checkout_path: str | None,
    check_links: bool,
) -> dict[str, Any]:
    """Return the CLI document audit findings without printing or mutating."""

    document = Path(path).expanduser()
    if not document.is_absolute():
        base = (
            Path(checkout_path).expanduser().resolve()
            if checkout_path is not None
            else Path.cwd()
        )
        document = base / document
    document = document.resolve()
    findings = audit_file(document, project=project)
    if check_links:
        findings += audit_links([document], document.parent, project=project).get(
            document, []
        )
        findings.sort(key=lambda item: SEVERITIES.index(item.severity))
    rows = [
        {"severity": item.severity, "code": item.code, "message": item.message}
        for item in findings
    ]
    counts = {
        "total": len(rows),
        "by_severity": _rollup_counts([item["severity"] for item in rows]),
        "by_category": {},
        "by_code": _rollup_counts([item["code"] for item in rows]),
    }
    errors = [item for item in rows if item["severity"] == "error"]
    return {
        "ok": not errors,
        "project": project or "",
        "path": str(document),
        "checked": 1,
        "conformant": 0 if errors else 1,
        "violations": (
            [{"slug": document.stem, "errors": [item["message"] for item in errors]}]
            if errors
            else []
        ),
        "findings": rows,
        "finding_counts": counts,
        "rollups_recomputed": False,
        "reindexed": False,
    }


def _audit_plan_state(path: Path) -> PlanState:
    """Return the lenient :class:`PlanState` for one authored document.

    ``_plan_html.from_html`` reads and parses the whole document on every call,
    so the audit's validation loop re-parsed every unchanged plan on every warm
    read. Keying the parse on the file's stat identity through
    :func:`reckon.file_memo.memoized` reuses it while the bytes are unchanged —
    moving size, mtime or inode re-parses — and reuses the shared text read so
    the file itself is opened once. The memo hands back a deep copy, so a
    caller that mutates the state cannot poison the next reader.
    """
    from reckon.file_memo import memoized

    return memoized(
        "audit_plan_state",
        path,
        lambda: _plan_html.from_html(_plan_html._read_plan_text(path)),
    )


def _audit(
    project: str | None = None,
    checkout_path: str | None = None,
    view: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    path: str | None = None,
    check_links: bool = False,
) -> dict[str, Any]:
    """Audit every plan in a project against the PlanState schema (the WARN half
    of reject-write-warn-doctor) and recompute the index rollups.

    For each plan HTML, parse it leniently then run validate_for_write semantics
    NON-RAISINGLY, collecting any messages. Recomputes sprint/milestone/projects
    rollups in the response (inventory[] stays synthesised live — not persisted).
    Returns { project, checked, conformant, violations:[{slug, errors}],
    rollups_recomputed: True, reindexed: False }.

    Pass ``path`` to validate one authored HTML document with the same checks as
    ``reckon audit-doc``; ``check_links`` adds its corpus-aware link checks.

    WARN/report ONLY — this NEVER mutates a plan or writes index.json. (Distinct
    from the CLI `reckon doctor`, which checks infra/skills/mounts, not schema.)
    With ``checkout_path``, the audit runs against that checkout's docs/state
    instead of the mounts-registered main checkout. Pass ``view="summary"`` for
    compact counts, ``view="detail"`` for paginated findings, or ``view="raw"``
    for the exact legacy audit payload. Omitting ``view`` preserves the legacy
    response unchanged.
    """
    if path is not None:
        raw = _audit_document(
            path,
            project=project,
            checkout_path=checkout_path,
            check_links=check_links,
        )
        if view is None:
            return raw
        try:
            return audit_view(
                project or "document",
                raw,
                view=view,
                cursor=cursor,
                limit=limit,
            )
        except ViewRequestError as exc:
            return error_response(exc.code, exc.message, hint=exc.hint)
    if not project:
        return {
            "ok": False,
            "error": "missing_project_or_path",
            "detail": "audit needs project or path",
        }
    if view is not None:
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
            state = _audit_plan_state(html_file)
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
    findings.extend(_tag_audit_findings(project, _tag_inventory(plans)))
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
    for invalid_path, message in invalid_resources:
        findings.append(
            _finding(
                "resources",
                "invalid-resource-path",
                "error",
                message,
                path=invalid_path,
            )
        )
    for slug, compatibility_path, warning in compatibility_records:
        findings.append(
            _finding(
                "compatibility",
                "legacy-capability-tier",
                "warn",
                warning,
                slug=slug,
                path=compatibility_path,
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
        for linked_path, path_findings in link_findings.items():
            rel = str(linked_path.relative_to(docs_dir))
            slug = linked_path.stem
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
        docs_dir=docs_dir,
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
        "summary": _discovery_summary(
            project,
            plans,
            followups,
            questions,
            sprints=list(discovered.get("sprints") or []),
            all_plans=plan_lookup,
            docs_dir=docs_dir,
        ),
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


def _audit_tool(
    project: str | None = None,
    checkout_path: str | None = None,
    view: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    path: str | None = None,
    check_links: bool = False,
) -> dict[str, Any]:
    """Return a compact project audit by default and preserve document reads.

    Project audits default to ``summary`` because their lossless finding lists
    routinely exceed the MCP transport. Request ``view='raw'`` explicitly for
    those lists. A path without a view still returns that document's own exact
    findings, matching the command-line document audit.
    """

    selected_view = view
    if view is None and path is None and project is not None:
        selected_view = "summary"
    if checkout_path is None:
        checkout_path = _run_scoped_read_root(project)
    return _audit(
        project=project,
        checkout_path=checkout_path,
        view=selected_view,
        cursor=cursor,
        limit=limit,
        path=path,
        check_links=check_links,
    )


def _field_declares_list(annotation: Any) -> bool:
    """Return whether a pydantic field annotation declared the value as a list.

    Unwraps ``Annotated`` and the ``X | None`` optional form so a nullable
    list field — the shape every list argument on this surface uses — is
    still recognised.
    """

    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if origin in (Union, UnionType):
            members = [a for a in get_args(annotation) if a is not type(None)]
            if len(members) == 1:
                annotation = members[0]
                continue
            return False
        return origin is list


def _reject_unknown_tool_arguments(tool_name: str) -> None:
    """Make one FastMCP entry refuse misspelled parameters before dispatch.

    The strict wrapper also accepts, for every argument the model declares as
    a list, a JSON text that parses to a list: a client may serialise a list
    argument into a string, and the parsed list validates identically to one
    delivered as a list. Only a list argument is treated this way — a text
    argument whose content happens to parse as JSON is passed through
    untouched, text that does not parse is left for normal field validation,
    and unknown parameter names are still rejected.
    """

    from pydantic import ConfigDict, model_validator

    tool = next(
        item for item in mcp._tool_manager.list_tools() if item.name == tool_name
    )
    argument_model = tool.fn_metadata.arg_model
    accepted = tuple(argument_model.model_fields)
    accepted_text = ", ".join(accepted)
    list_arguments = frozenset(
        name
        for name, field in argument_model.model_fields.items()
        if _field_declares_list(field.annotation)
    )

    class StrictArguments(argument_model):
        model_config = ConfigDict(
            **dict(argument_model.model_config),
            extra="forbid",
        )

        @model_validator(mode="before")
        @classmethod
        def reject_unknown(cls, value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            unknown = sorted(set(value) - set(accepted))
            if unknown:
                names = ", ".join(unknown)
                raise ValueError(
                    f"Unknown parameters: {names}. "
                    f"Accepted parameters: {accepted_text}."
                )
            decoded = None
            for argument in list_arguments:
                candidate = value.get(argument)
                if not isinstance(candidate, str):
                    continue
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, list):
                    continue
                if decoded is None:
                    decoded = dict(value)
                decoded[argument] = parsed
            return decoded if decoded is not None else value

    tool.fn_metadata.arg_model = StrictArguments
    tool.parameters = StrictArguments.model_json_schema(by_alias=True)


# ── Register tools with SDK ────────────────────────────────────────────────
#
# Agent-facing MCP surface = read_plan + edit_plan + roadmap + audit + crew.
# The granular _funcs below remain for tests/internal use but are
# intentionally NOT registered (collapsed per the schema-and-tooling plan);
# full removal is a later cleanup. read_plan folds the 5 legacy reads
# (list_plans/list_projects/list_sprints/list_followups/list_questions) via
# its discovery + with_schema modes; edit_plan folds the granular mutators via
# its set/append/resolve/lock/move + create ops; crew reads run state over
# several views plus the resume, session and resume-ready recovery actions, so a
# coordinator can answer a provider refusal without shelling out to the CLI.

if mcp is not None:
    read_plan_tool = mcp.tool(name="read_plan")(
        _deadline_tool(
            _read_plan_tool,
            kind="read",
            path_hint=_plan_path_hint,
            response_budget=True,
        )
    )
    edit_plan_tool = mcp.tool(name="edit_plan")(
        _deadline_tool(_edit_plan_tool, kind="write", path_hint=_plan_path_hint)
    )
    roadmap_tool = mcp.tool(name="roadmap")(
        _deadline_tool(_roadmap_tool, kind="read", response_budget=True)
    )
    audit_tool = mcp.tool(name="audit")(
        _deadline_tool(
            _audit_tool,
            kind="read",
            path_hint=_document_path_hint,
            response_budget=True,
        )
    )
    crew_tool = mcp.tool(name="crew")(
        _deadline_tool(_crew, kind=_crew_call_kind, response_budget=True)
    )
    for tool_name in ("read_plan", "edit_plan", "roadmap", "audit", "crew"):
        _reject_unknown_tool_arguments(tool_name)


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
