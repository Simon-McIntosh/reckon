from __future__ import annotations

import shlex
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# ── SDK import ─────────────────────────────────────────────────────────────
from reckon import (
    _plan_html,
    roadmap,
)
from reckon._schema import (
    PLAN_STANDALONE_META,
)
from reckon._store import (
    _docs_dir_for_project,
    _resolve_html_file,
    _state_root,
    read_plan,
)
from reckon.mcp_views import (
    index_discovery,
)
from reckon.resources import (
    resource_map,
)
from reckon.serve import discover_plans, edge_row


def _discovery_state_root(root: str | None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve() / "docs" / "state"
    return _state_root()


def _index_project_summary(
    project: str,
    checkout_path: str | None,
    *,
    status: str | None = None,
    doc_type: str | None = None,
    sprint: str | None = None,
    milestone: str | None = None,
    owner: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a project read's list-level payload, taken from the index.

    The per-document body state the derived payload once parsed every plan for
    is not listed here: a summary names which documents exist and the project's
    own sprint, milestone, blocker and timeline state, and a read of one
    document derives that document.
    """

    docs_dir = _docs_dir_for_project(project, checkout_path)
    if docs_dir is None:
        return {"inventory": [], "sprints": [], "milestones": []}
    discovered = index_discovery(
        docs_dir, project, _discovery_state_root(checkout_path)
    )
    inventory = list(discovered.get("inventory") or [])
    plans = _filter_inventory(
        [_inventory_row(item) for item in inventory],
        status=status,
        doc_type=doc_type,
        sprint=sprint,
        milestone=milestone,
        owner=owner,
        search=None,
        limit=limit,
    )
    sprints = list(discovered.get("sprints") or [])
    active_sprint_id = discovered.get("active_sprint_id")
    if not active_sprint_id:
        active_sprint_id = next(
            (
                item.get("id")
                for item in sprints
                if isinstance(item, dict) and item.get("status") == "active"
            ),
            None,
        )
    summary = _discovery_summary(
        project,
        plans,
        [],
        [],
        sprints=sprints,
        all_plans={
            str(item.get("slug")): item for item in inventory if item.get("slug")
        },
        docs_dir=docs_dir,
    )
    # A count that needs the documents' bodies is dropped rather than reported
    # as zero: the read that derives it names it.
    for key in ("open_followups", "open_questions", "open_decisions", "impl_mean"):
        summary.pop(key, None)
    return {
        "project": project,
        "plans": plans,
        "followups": [],
        "questions": [],
        "sprints": sprints,
        "milestones": list(discovered.get("milestones") or []),
        "blockers": list(discovered.get("blockers") or []),
        "timeline": list(discovered.get("timeline") or []),
        "active_sprint_id": active_sprint_id,
        "source_format": discovered.get("source_format", "legacy-index"),
        "resource_versions": discovered.get("resource_versions", {}),
        "tag_inventory": _tag_inventory(inventory),
        "summary": summary,
    }


def _aggregate_version(project: str, root: str | None = None) -> int:
    """Return the project's aggregate-state version, without deriving it.

    A sprint read answers from the index and still reports the concurrency
    token its resource shares with the project's aggregate state document, so
    the version comes from that one document rather than a tree scan.
    """

    try:
        _data, version = read_plan(project, "index", root)
    except Exception:  # noqa: BLE001 — an absent aggregate reads as version zero
        return 0
    return int(version or 0)


def _index_discovery(project: str, root: str | None = None) -> dict[str, Any]:
    """Return the project's list-level discovery, taken from the index.

    A sprint read and a summary or detail read of one plan need the inventory
    rows the index already holds — for a sprint, the items to hydrate; for a
    plan, the derived blocking the list carries — rather than the document
    bodies a corpus walk parses. The rows come from the persisted index and the
    project's own state document, so neither read walks the corpus.
    """

    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return {"inventory": [], "sprints": [], "milestones": []}
    return index_discovery(docs_dir, project, _discovery_state_root(root))


def _discover_project(project: str, root: str | None = None) -> dict[str, Any]:
    docs_dir = _docs_dir_for_project(project, root)
    if docs_dir is None:
        return {"inventory": [], "sprints": [], "milestones": []}
    discovered = discover_plans(docs_dir, project, _discovery_state_root(root))

    resources = {
        resource.identity.key: resource
        for resource in resource_map(
            docs_dir,
            project,
            include_archived=True,
            ignore_invalid=True,
        ).values()
    }
    inventory = []
    for item in discovered.get("inventory", []):
        resource = resources.get(str(item.get("resource_id") or ""))
        meta = _plan_html.parse_meta(resource.path) if resource is not None else {}
        inventory.append(
            {
                **item,
                "tags": list(meta.get("tags") or []),
                "graph_handle": meta.get("graph_handle"),
            }
        )
    return {**discovered, "inventory": inventory}


#: Op path carrying a plan's standalone declaration. It is authored markup
#: rather than a state field, so it is applied to the header directly instead
#: of through ``apply_ops``, which owns only the fields it can round-trip.
_STANDALONE_SET_PATH = "standalone"


def _extract_standalone_declaration(
    ops: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Split a ``set standalone`` op out of ``ops``.

    Returns ``(remaining_ops, declaration)`` where ``declaration`` is ``None``
    when the op is absent, ``""`` when it clears the meta, and the reason text
    otherwise — so "not mentioned" is never confused with "declared empty".
    """

    remaining: list[dict[str, Any]] = []
    declaration: str | None = None
    for op in ops or []:
        if (
            isinstance(op, dict)
            and op.get("op") == "set"
            and op.get("path") == _STANDALONE_SET_PATH
        ):
            value = op.get("value")
            declaration = str(value).strip() if value is not None else ""
            continue
        remaining.append(op)
    return remaining, declaration


def _apply_standalone_meta(html_text: str, reason: str) -> str:
    """Set or clear the ``plan-standalone`` meta in a plan's header."""

    if reason.strip():
        return _plan_html._set_meta(html_text, PLAN_STANDALONE_META, reason.strip())
    return _plan_html._remove_meta(html_text, PLAN_STANDALONE_META)


def _unwired_plan_refusal(slug: str, working: Mapping[str, Any]) -> str | None:
    """The refusal message for a new plan that declares no wire at all."""

    from reckon.doccheck import unwired_plan_finding

    finding = unwired_plan_finding(
        doc_type="plan",
        status=str(working.get("status") or "").strip().lower(),
        modified=str(working.get("modified") or ""),
        links=(
            list(working.get("depends_on") or [])
            + list(working.get("blocks") or [])
            + list(working.get("informs") or [])
        ),
        gate_count=len(working.get("gates") or []),
        standalone=working.get("standalone"),
        slug=slug,
    )
    return finding.message if finding is not None else None


def _plan_html_text(
    project: str,
    slug: str,
    checkout_path: str | None,
    doc_type: str | None,
) -> str:
    """Read a plan's own HTML, for declarations the state read does not carry."""

    try:
        path = _resolve_html_file(project, slug, checkout_path, doc_type or "plan")
    except Exception:  # noqa: BLE001 — an unresolvable plan carries no declaration
        return ""
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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
        "tags": list(item.get("tags") or []),
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
                "graph_handle": item.get("graph_handle"),
                "north_star": item.get("north_star"),
                "roi": item.get("roi"),
                "effort": item.get("effort"),
                # Both effort quantities must survive into the inventory: the
                # roadmap falls back to the legacy letter map when they are
                # absent, which silently replaces an authored estimate with the
                # default for its size letter.
                "effort_hours": item.get("effort_hours"),
                "wall_clock_hours": item.get("wall_clock_hours"),
                "effort_calibrated": item.get("effort_calibrated"),
                "capability": item.get("capability"),
                "tier": item.get("tier"),
                "dec_open": int(item.get("dec_open", 0) or 0),
                "decisions": list(item.get("decisions") or []),
                "blockers": int(item.get("blockers", 0) or 0),
                "blocking": list(item.get("blocking") or []),
                "gates": list(item.get("gates") or []),
                "followups": list(item.get("followups") or []),
                **edge_row(item),
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
            "body_text",
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


def _tag_inventory(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count live resources carrying each observed tag identity."""

    counts: Counter[str] = Counter()
    for item in inventory:
        if item.get("archived"):
            continue
        counts.update(tuple(dict.fromkeys(str(tag) for tag in item.get("tags") or [])))
    return [{"tag": tag, "count": counts[tag]} for tag in sorted(counts)]


def _bounded_edit_distance(left: str, right: str, limit: int) -> int:
    """Return edit distance, stopping once the requested bound is exceeded."""

    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        row_minimum = left_index
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
            row_minimum = min(row_minimum, current[-1])
        if row_minimum > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _tag_audit_findings(
    project: str, tag_inventory: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Surface sparse and suspiciously similar tag identities without mutation."""

    findings = [
        _finding(
            "tags",
            "tag-singleton",
            "warn",
            f"tag {item['tag']!r} is used by 1 resource",
            extra={"tag": item["tag"], "count": 1},
        )
        for item in tag_inventory
        if item["count"] == 1
    ]
    for position, left in enumerate(tag_inventory):
        for right in tag_inventory[position + 1 :]:
            shorter_length = min(len(left["tag"]), len(right["tag"]))
            distance_limit = 2 if shorter_length >= 10 else 1
            distance = _bounded_edit_distance(left["tag"], right["tag"], distance_limit)
            if distance > distance_limit:
                continue
            target, source = sorted(
                (left, right), key=lambda item: (-item["count"], item["tag"])
            )
            invocation = " ".join(
                shlex.quote(part)
                for part in (
                    "reckon",
                    "tag",
                    "rename",
                    "--project",
                    project,
                    source["tag"],
                    target["tag"],
                )
            )
            findings.append(
                _finding(
                    "tags",
                    "tag-near-duplicate",
                    "warn",
                    (
                        f"tags {left['tag']!r} ({left['count']}) and "
                        f"{right['tag']!r} ({right['count']}) differ by edit "
                        f"distance {distance}; merge with: {invocation}"
                    ),
                    extra={
                        "tags": [
                            {"tag": left["tag"], "count": left["count"]},
                            {"tag": right["tag"], "count": right["count"]},
                        ],
                        "distance": distance,
                        "rename_invocation": invocation,
                    },
                )
            )
    return findings


def _summary_sprint_rows(
    project: str,
    sprints: list[dict[str, Any]] | None,
    all_plans: Mapping[str, dict[str, Any]] | None,
    *,
    docs_dir: str | Path | None,
    recent_days: int | None,
) -> list[dict[str, Any]]:
    """The compact sprint list a discovery summary carries.

    The rows come from one shared derivation — :func:`roadmap.sprint_summary_rows`
    — so the discovery and roadmap summaries answer the same question the same
    way rather than each building its own. The window is the caller's when it
    passed one, otherwise :func:`roadmap._sprint_recent_days` resolves it from the
    project's flight config so both surfaces window through one resolver.
    """
    if recent_days is None:
        recent_days = roadmap._sprint_recent_days(project, docs_dir)
    return roadmap.sprint_summary_rows(
        project,
        list(sprints or []),
        all_plans or {},
        recent_days=recent_days,
        docs_dir=docs_dir,
    )


def _discovery_summary(
    project: str,
    plans: list[dict[str, Any]],
    followups: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    *,
    sprints: list[dict[str, Any]] | None = None,
    all_plans: Mapping[str, dict[str, Any]] | None = None,
    docs_dir: str | Path | None = None,
    recent_days: int | None = None,
) -> dict[str, Any]:
    actionable = [item for item in plans if item.get("type", "plan") == "plan"]
    sprint_values = [plan.get("sprint") or "—" for plan in actionable]
    milestone_values = [
        plan.get("milestone") or plan.get("ms") or "—" for plan in actionable
    ]
    impl_values = [float(plan.get("impl", 0.0) or 0.0) for plan in actionable]
    summary = {
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
    sprint_rows = _summary_sprint_rows(
        project,
        sprints,
        all_plans,
        docs_dir=docs_dir,
        recent_days=recent_days,
    )
    if sprint_rows:
        summary["summary_sprints"] = sprint_rows
    return summary


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
    if len(active_ids) == 1:
        pushed_id = active_ids[0]
        pushed_items = (sprint_map.get(pushed_id) or {}).get("items", []) or []
        ready_on_pushed = [
            slug
            for item in pushed_items
            if (slug := _sprint_item_slug(item))
            and slug in plan_map
            and str(plan_map[slug].get("status") or "").lower()
            not in terminal_plan_statuses
            and float(plan_map[slug].get("impl", 0.0) or 0.0) < 1.0
        ]
        if not ready_on_pushed:
            findings.append(
                _finding(
                    "sprint",
                    "pushed-sprint-has-no-ready-work",
                    "warn",
                    (
                        f"pushed sprint {pushed_id!r} holds no plan with ready work; "
                        "the pushed path has nothing for dispatch to pick up"
                    ),
                    extra={"sprint_id": pushed_id},
                )
            )

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
            # A plan absent from an open sprint's items is untidy; a plan
            # declaring a CLOSED sprint will never be scheduled at all. Same
            # condition, two severities, and one badge for both is why a session
            # walked past this warning twice on its own defect.
            declared_status = str(
                (sprint_map.get(plan_sprint) or {}).get("status") or ""
            ).lower()
            if declared_status in roadmap.CLOSED_SPRINT_STATUSES:
                findings.append(
                    _finding(
                        "sprint",
                        "plan-sprint-closed",
                        "error",
                        (
                            f"plan metadata assigns sprint {plan_sprint!r}, which is "
                            f"{declared_status} — this work is invisible to the "
                            "advancing horizon until it moves to an open sprint"
                        ),
                        slug=slug,
                        extra={"sprint_id": plan_sprint, "status": declared_status},
                    )
                )
            else:
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
