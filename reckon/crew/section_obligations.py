"""Account for merged implementations whose sections are still implementable."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from reckon import _store, ledger
from reckon._plan_html import section_record_id
from reckon._schema import parse_plan_ref
from reckon._timestamps import parse_utc
from reckon.crew.dispatch_sections import project_mount_repository
from reckon.crew.node import normalize_section
from reckon.crew.promotion_scope import _revision_is_ancestor
from reckon.evidence import EXECUTABLE_SECTION_ROLES

DECLARATION_DUTY_KIND = "section-declaration-owed"

# A coordinator records a partial delivery as a section comment through the
# ordinary plan writer. The run id scopes that judgement to this delivery, so
# another merged node in the same section still owes its own accounting.
PARTIAL_DELIVERY_COMMENT_PREFIX = "c-partial-"


def merged_section_items_by_session(
    project: str,
    pointers: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    session: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """One duty per merged, unaccounted section owned by this session.

    A declaration remains the release authority. Reachability supplies evidence
    for the duty, never permission to advance the declaration or its waiters.
    Plan reads and reachability answers are shared across this derivation.
    """
    checkout = project_mount_repository(project)
    if checkout is None:
        return {}
    history, _version = ledger.load(project, checkout)
    candidates = {
        str(record.get("run_id") or ""): record
        for record in [*pointers, *history["runs"]]
        if record.get("session") and (session is None or record["session"] == session)
    }
    plans: dict[str, tuple[dict, int]] = {}
    reachable: dict[str, bool] = {}
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for run_id, record in candidates.items():
        node = record.get("node")
        node = node if isinstance(node, Mapping) else {}
        role = str(node.get("role") or record.get("role") or "implement")
        plan = str(node.get("plan") or record.get("plan") or "")
        section = section_record_id(node.get("section") or record.get("section"))
        revision = str(record.get("promoted_revision") or "").strip()
        if not (run_id and plan and section and revision):
            continue
        if role not in EXECUTABLE_SECTION_ROLES:
            continue
        if plan not in plans:
            plans[plan] = _store.read_plan(project, plan, checkout)
        state, _version = plans[plan]
        declaration = next(
            (
                value
                for key, value in (state.get("section_declarations") or {}).items()
                if section_record_id(key) == section
            ),
            None,
        )
        if declaration != "implementable":
            continue
        partial_id = PARTIAL_DELIVERY_COMMENT_PREFIX + run_id
        if any(
            comment.get("id") == partial_id and str(comment.get("body") or "").strip()
            for key, comments in (state.get("comments") or {}).items()
            if section_record_id(key) == section
            for comment in comments
        ):
            continue
        if revision not in reachable:
            reachable[revision] = _revision_is_ancestor(checkout, revision, "HEAD")
        if reachable[revision]:
            groups[str(record["session"]), plan, section].append(
                {
                    **record,
                    "run_id": run_id,
                    "head": revision,
                    "node": str(node.get("id") or record.get("node") or ""),
                }
            )

    waiters: dict[tuple[str, str], set[str]] = defaultdict(set)
    for pointer in pointers:
        if pointer.get("phase") not in {"awaiting-upstream", "blocked-upstream"}:
            continue
        for ref in pointer.get("waits") or ():
            parsed = parse_plan_ref(str(ref))
            if parsed and parsed.stage and (parsed.project or project) == project:
                key = (parsed.slug, section_record_id(parsed.stage))
                waiters[key].add(str(pointer.get("run_id") or ""))

    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (owner, plan, section), merged in sorted(groups.items()):
        record = merged[-1]
        run_id = record["run_id"]
        waiting = sorted(waiters[plan, section] - {""})
        state, version = plans[plan]
        declaration_key = next(
            key
            for key in state["section_declarations"]
            if section_record_id(key) == section
        )
        done_ops = [
            {
                "op": "set",
                "path": f"section_declarations.{declaration_key}",
                "value": "done",
            }
        ]
        partial_ops = [
            {
                "op": "append",
                "target": "comments",
                "section": section,
                "item": {
                    "id": PARTIAL_DELIVERY_COMMENT_PREFIX + row["run_id"],
                    "who": owner,
                    "when": now.isoformat(),
                    "body": f"Run {row['run_id']} delivered only part of this section; remaining work stays implementable.",
                },
            }
            for row in merged
        ]
        command_prefix = f"edit_plan(project={project!r}, slug={plan!r}, expected_version={version}, "
        completed = parse_utc(record.get("completed_at"))
        items[owner].append(
            {
                "kind": DECLARATION_DUTY_KIND,
                "run_id": run_id,
                "node": str(record.get("node") or ""),
                "plan": plan,
                "section": section,
                "head": record["head"],
                "merged_run_ids": [row["run_id"] for row in merged],
                "waiting_requests": waiting,
                "held_count": len(waiting),
                "age_seconds": max(0, int((now - completed).total_seconds()))
                if completed
                else 0,
                "reason": (
                    f"Merged run(s) {', '.join(row['run_id'] for row in merged)} reached "
                    f"the primary checkout while {plan}#{section} remains implementable; "
                    f"waiting requests: {', '.join(waiting) or 'none'}. Record the "
                    "section done or deferred, or record a partial delivery for the named runs."
                ),
                "next_command": f"/reckon-edit {plan} {normalize_section(section)}",
                "next_action_form": command_prefix + f"ops={done_ops!r})",
                "partial_action_form": command_prefix + f"ops={partial_ops!r})",
            }
        )
    return items


def merged_section_items(
    project: str,
    session: str,
    pointers: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """One session's slice of the shared section-accounting derivation."""
    return merged_section_items_by_session(
        project, pointers, now=now, session=session
    ).get(session, [])
