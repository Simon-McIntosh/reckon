"""Field-level preservation evidence for distributed project state.

The completion marker names a frozen aggregate snapshot.  That snapshot is
the authority for aggregate fields; historical plan state is compared only
when the snapshot contains an explicit plan inventory.  Fields absent from
that corpus are reported as ``out-of-corpus`` and never credited as matches.

The report also names two deliberately excluded derived observations:
``blocks-descriptive-not-gating`` and ``new-wiring-visibility``.  The authored
``blocks`` values themselves are still compared exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from reckon import _plan_html
from reckon.project_state import (
    ProjectStateError,
    _parity_projection,
    compose_project_state,
    project_state_mode,
)
from reckon.resources import resource_map

RELATIONAL_FIELDS = (
    "depends_on",
    "blocks",
    "informs",
    "evidence_for",
    "verifies",
    "supersedes",
)
ORGANISATIONAL_FIELDS = ("sprint", "milestone", "north_star", "tier")
NESTED_FIELDS = ("decisions", "followups", "comments", "questions", "research")
SPRINT_ITEM_FIELDS = ("sprint_items", "why_now", "done_when")
EXCLUDED_OBSERVATIONS = (
    "blocks-descriptive-not-gating",
    "new-wiring-visibility",
)

_IGNORED_PLAN_INVENTORY_FIELDS = frozenset(
    {
        "archived",
        "href",
        "path",
        "resource_id",
        "source_path",
    }
)
_IGNORED_SPRINT_ITEM_FIELDS = frozenset(
    {"status", "impl", "title", "effort_hours", "effort_calibrated"}
)


@dataclass(frozen=True)
class FrozenProjectState:
    """A validated marker-selected aggregate snapshot."""

    project: str
    marker_path: Path
    snapshot_path: Path
    marker: dict[str, Any]
    data: dict[str, Any]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _contained_path(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ProjectStateError(f"distributed marker has no {label}")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ProjectStateError(f"distributed marker {label} must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ProjectStateError(f"distributed marker {label} escapes docs directory")
    return resolved


def resolve_frozen_snapshot(
    docs_dir: Path, project: str | None = None
) -> FrozenProjectState:
    """Resolve and verify the raw snapshot named by a distributed marker."""
    docs_dir = docs_dir.resolve()
    mode = project_state_mode(docs_dir)
    if mode.format != "distributed" or mode.marker is None:
        raise ProjectStateError("project docs are not in complete distributed mode")
    marker = mode.marker
    marker_project = marker.get("project")
    if not isinstance(marker_project, str) or not marker_project:
        raise ProjectStateError("distributed marker has no project identity")
    if project is not None and project != marker_project:
        raise ProjectStateError(
            f"requested project {project!r} does not match marker {marker_project!r}"
        )
    snapshot_path = _contained_path(docs_dir, marker.get("snapshot"), "snapshot")
    if not snapshot_path.is_file():
        raise ProjectStateError(f"distributed snapshot does not exist: {snapshot_path}")
    raw = snapshot_path.read_bytes()
    expected_hash = marker.get("snapshot_sha256")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise ProjectStateError("distributed marker has no snapshot_sha256")
    actual_hash = _sha256(raw)
    if actual_hash != expected_hash:
        raise ProjectStateError(
            "distributed snapshot hash mismatch: "
            f"expected {expected_hash}, got {actual_hash}"
        )
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectStateError(
            f"distributed snapshot is malformed JSON: {snapshot_path}"
        ) from exc
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        raise ProjectStateError(
            f"distributed snapshot has no object-valued data: {snapshot_path}"
        )
    envelope_project = envelope.get("project")
    if envelope_project not in (None, "", marker_project):
        raise ProjectStateError(
            "distributed snapshot project does not match marker: "
            f"{envelope_project!r} != {marker_project!r}"
        )
    return FrozenProjectState(
        project=marker_project,
        marker_path=docs_dir / ".reckon" / "project-state-migration.json",
        snapshot_path=snapshot_path,
        marker=marker,
        data=data,
    )


def _normalise(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalise(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def _plan_inventory(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract explicit historical plan states without inferring missing data."""
    raw = data.get("plans", data.get("inventory"))
    rows: Iterable[Any]
    if isinstance(raw, Mapping):
        rows = (
            {**value, "slug": value.get("slug") or key}
            if isinstance(value, Mapping)
            else value
            for key, value in raw.items()
        )
    elif isinstance(raw, list):
        rows = raw
    else:
        return {}
    plans: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            continue
        state = raw_row.get("state")
        row = dict(state) if isinstance(state, Mapping) else dict(raw_row)
        resource_type = str(row.get("type") or raw_row.get("type") or "plan")
        if resource_type != "plan":
            continue
        slug = str(row.get("slug") or raw_row.get("slug") or "")
        if not slug:
            continue
        plans[slug] = {
            key: value
            for key, value in row.items()
            if key not in _IGNORED_PLAN_INVENTORY_FIELDS
        }
    return plans


def _live_plans(docs_dir: Path, project: str) -> dict[str, dict[str, Any]]:
    plans: dict[str, dict[str, Any]] = {}
    for resource in resource_map(
        docs_dir,
        project,
        include_archived=False,
        ignore_invalid=True,
    ).values():
        if resource.type != "plan":
            continue
        plans[resource.slug] = _plan_html.read_state(
            resource.path.read_text(encoding="utf-8", errors="strict")
        )
    return plans


def _field_values(
    plans: Mapping[str, Mapping[str, Any]], field: str
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for slug, state in sorted(plans.items()):
        value = state.get(field)
        if value in (None, "", [], {}):
            continue
        values[f"plan:{slug}"] = _normalise(value)
    return values


def _walk_field(value: Any, field: str, path: str = "") -> dict[str, Any]:
    found: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key == field and child not in (None, "", [], {}):
                found[child_path] = _normalise(child)
            found.update(_walk_field(child, field, child_path))
    elif isinstance(value, list):
        for position, child in enumerate(value):
            identity = position
            if isinstance(child, Mapping):
                identity = child.get("id") or child.get("slug") or position
            found.update(_walk_field(child, field, f"{path}[{identity}]"))
    return found


def _sprint_membership(data: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for sprint in data.get("sprints", []):
        if not isinstance(sprint, Mapping):
            continue
        sprint_id = str(sprint.get("id") or "")
        for raw_item in sprint.get("items", []):
            item = {"slug": raw_item} if isinstance(raw_item, str) else raw_item
            if not isinstance(item, Mapping):
                continue
            slug = str(item.get("slug") or "")
            if slug:
                values[f"item:{slug}"] = sprint_id
    return values


def _sprint_items(data: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for sprint in data.get("sprints", []):
        if not isinstance(sprint, Mapping):
            continue
        sprint_id = str(sprint.get("id") or "")
        for position, raw_item in enumerate(sprint.get("items", [])):
            item = {"slug": raw_item} if isinstance(raw_item, str) else raw_item
            if not isinstance(item, Mapping):
                continue
            slug = str(item.get("slug") or position)
            retained = {
                key: value
                for key, value in item.items()
                if key not in _IGNORED_SPRINT_ITEM_FIELDS
            }
            values[f"sprint:{sprint_id}/item:{slug}"] = _normalise(retained)
    return values


def _sprint_item_field(data: Mapping[str, Any], field: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for sprint in data.get("sprints", []):
        if not isinstance(sprint, Mapping):
            continue
        sprint_id = str(sprint.get("id") or "")
        for position, raw_item in enumerate(sprint.get("items", [])):
            item = {"slug": raw_item} if isinstance(raw_item, str) else raw_item
            if not isinstance(item, Mapping) or field not in item:
                continue
            slug = str(item.get("slug") or position)
            values[f"sprint:{sprint_id}/item:{slug}"] = _normalise(item[field])
    return values


def _allowed_drift(
    path: str,
    before: Any,
    after: Any,
    drift_fields: frozenset[str],
) -> list[dict[str, Any]] | None:
    if (
        not drift_fields
        or not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
    ):
        return None
    missing = object()
    changed = [
        field
        for field in sorted(set(before) | set(after))
        if before.get(field, missing) != after.get(field, missing)
    ]
    if not changed or not set(changed).issubset(drift_fields):
        return None
    return [
        {
            "path": path,
            "field": field,
            "before": before.get(field, "<missing>"),
            "after": after.get(field, "<missing>"),
        }
        for field in changed
    ]


def _comparison(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    drift_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    additional = len(set(after) - set(before))
    if not before:
        return {
            "status": "out-of-corpus",
            "compared": 0,
            "matched": 0,
            "additional": additional,
            "current_state_drift": 0,
            "drift_details": [],
            "mismatches": [],
        }
    paths = sorted(before)
    missing = object()
    mismatches = []
    drift_details = []
    matched = 0
    for path in paths:
        old = before.get(path, missing)
        new = after.get(path, missing)
        if old == new:
            matched += 1
            continue
        drift = _allowed_drift(path, old, new, drift_fields)
        if drift is not None:
            drift_details.extend(drift)
            continue
        mismatches.append(
            {
                "path": path,
                "before": "<missing>" if old is missing else old,
                "after": "<missing>" if new is missing else new,
            }
        )
    return {
        "status": (
            "mismatch"
            if mismatches
            else "preserved-with-current-state-drift"
            if drift_details
            else "matched"
        ),
        "compared": len(paths),
        "matched": matched,
        "additional": additional,
        "current_state_drift": len(drift_details),
        "drift_details": drift_details,
        "mismatches": mismatches,
    }


def compare_project_state(docs_dir: Path, project: str | None = None) -> dict[str, Any]:
    """Compare a distributed tree with its marker-selected frozen snapshot."""
    docs_dir = docs_dir.resolve()
    frozen = resolve_frozen_snapshot(docs_dir, project)
    composed = compose_project_state(docs_dir, frozen.project)
    before_aggregate = _parity_projection(frozen.data, frozen.project)
    after_aggregate = _parity_projection(composed, frozen.project)
    before_plans = _plan_inventory(frozen.data)
    after_plans = _live_plans(docs_dir, frozen.project)

    fields: dict[str, dict[str, Any]] = {}
    for field in RELATIONAL_FIELDS:
        fields[field] = _comparison(
            _field_values(before_plans, field),
            _field_values(after_plans, field),
        )
    for field in ORGANISATIONAL_FIELDS:
        old_plan_values = _field_values(before_plans, field)
        new_plan_values = (
            _field_values(after_plans, field) if old_plan_values else {}
        )
        if field == "sprint":
            old_values = {
                **_sprint_membership(before_aggregate),
                **old_plan_values,
            }
            new_values = {
                **_sprint_membership(after_aggregate),
                **new_plan_values,
            }
        else:
            old_values = {
                **_walk_field(before_aggregate, field),
                **old_plan_values,
            }
            new_values = {
                **_walk_field(after_aggregate, field),
                **new_plan_values,
            }
        fields[field] = _comparison(old_values, new_values)

    fields["sprint_items"] = _comparison(
        _sprint_items(before_aggregate),
        _sprint_items(after_aggregate),
        drift_fields=frozenset({"blocked_by"}),
    )
    for field in ("why_now", "done_when"):
        fields[field] = _comparison(
            _sprint_item_field(before_aggregate, field),
            _sprint_item_field(after_aggregate, field),
        )
    for field in NESTED_FIELDS:
        fields[field] = _comparison(
            _field_values(before_plans, field),
            _field_values(after_plans, field),
        )

    mismatch_count = sum(
        len(result["mismatches"]) for result in fields.values()
    )
    compared = sum(result["compared"] for result in fields.values())
    matched = sum(result["matched"] for result in fields.values())
    return {
        "ok": mismatch_count == 0,
        "project": frozen.project,
        "docs_dir": str(docs_dir),
        "snapshot": str(frozen.snapshot_path),
        "snapshot_sha256": frozen.marker["snapshot_sha256"],
        "excluded_observations": list(EXCLUDED_OBSERVATIONS),
        "fields": fields,
        "totals": {
            "compared": compared,
            "matched": matched,
            "additional": sum(result["additional"] for result in fields.values()),
            "current_state_drift": sum(
                result["current_state_drift"] for result in fields.values()
            ),
            "mismatches": mismatch_count,
            "out_of_corpus": sum(
                result["status"] == "out-of-corpus" for result in fields.values()
            ),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare distributed project state field by field with the frozen "
            "snapshot named by its completion marker."
        )
    )
    parser.add_argument("docs_dir", type=Path, help="project docs directory")
    parser.add_argument(
        "--project",
        help="optional project identity; must match the completion marker",
    )
    parser.add_argument(
        "--indent", type=int, default=2, help="JSON indentation (default: 2)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = compare_project_state(args.docs_dir, args.project)
    except (OSError, ProjectStateError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=args.indent))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=args.indent))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
