"""Distributed project-state storage, compatibility, and concurrency tests."""

from __future__ import annotations

import base64
import http.client
import json
import multiprocessing
import re
import subprocess
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html
from reckon import cli as cli_module
from reckon import mcp as mcp_module
from reckon._store import read_plan, write_plan
from reckon.project_state import (
    LegacyIndexReadOnly,
    ProjectStateConflict,
    ProjectStateError,
    _apply_migration_composed_derivations,
    _identity_manifest,
    _parity_projection,
    _sha256_path,
    _write_staged_resource,
    append_timeline_event,
    audit_project_state,
    compose_project_state,
    create_project_state,
    marker_path,
    move_sprint_item,
    read_resource,
    resource_path,
    write_resource,
)


def _concurrent_write(
    docs: str,
    barrier: multiprocessing.synchronize.Barrier,
    queue: multiprocessing.queues.Queue,
    summary: str,
) -> None:
    """Process target: race a same-version write against another process."""
    root = Path(docs)
    current, version = read_resource(root, "sample", "sprint", "current")
    barrier.wait()
    try:
        result = write_resource(
            root,
            "sample",
            "sprint",
            "current",
            {**current, "summary": summary},
            version,
        )
        queue.put(("ok", result))
    except ProjectStateConflict as exc:
        queue.put(("conflict", exc.current))


def _concurrent_read(
    docs: str,
    barrier: multiprocessing.synchronize.Barrier,
    queue: multiprocessing.queues.Queue,
) -> None:
    """Process target: trigger journal recovery concurrently."""
    barrier.wait()
    try:
        state, version = read_resource(Path(docs), "sample", "sprint", "current")
        queue.put(("ok", state.get("summary", ""), version))
    except Exception as exc:  # pragma: no cover - surfaced through queue
        queue.put(("error", str(exc)))


def _concurrent_status_update(
    docs: str,
    sprint_id: str,
    barrier: multiprocessing.synchronize.Barrier,
    queue: multiprocessing.queues.Queue,
) -> None:
    """Process target: update a different sprint at the same instant."""
    root = Path(docs)
    sprint, version = read_resource(root, "sample", "sprint", sprint_id)
    barrier.wait()
    try:
        new_version = write_resource(
            root,
            "sample",
            "sprint",
            sprint_id,
            {**sprint, "status": "active"},
            version,
        )
        queue.put(("ok", sprint_id, new_version))
    except ValueError as exc:
        queue.put(("rejected", sprint_id, str(exc)))


def _write_plan(
    docs: Path, slug: str, status: str, impl: float, *, archived: bool = False
) -> None:
    root = docs / "plans" / "archive" if archived else docs / "plans"
    path = root / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": slug,
                "title": slug.title(),
                "status": status,
                "impl": impl,
            },
        ),
        encoding="utf-8",
    )


def _legacy_index(docs: Path) -> Path:
    path = docs / "state" / "sample" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-07-29T00:00:00",
        "project": "sample",
        "doc": "index",
        "data": {
            "_version": 8,
            "active_sprint_id": "current",
            "projects": [
                {
                    "project": "sample",
                    "path": "/private/checkout/docs",
                    "owner": "owner",
                    "published": "example.invalid/sample",
                    "plans_count": 99,
                }
            ],
            "sprints": [
                {
                    "id": "earlier",
                    "theme": "Earlier",
                    "status": "done",
                    "items": [],
                },
                {
                    "id": "current",
                    "theme": "Current",
                    "status": "active",
                    "items": [
                        {
                            "slug": "alpha",
                            "status": "pending",
                            "impl": 0,
                            "blocked_by": ["network"],
                            "why_now": "now",
                        }
                    ],
                },
            ],
            "milestones": [{"id": "launch", "name": "Launch", "status": "active"}],
            "blockers": [
                {
                    "id": "network",
                    "summary": "Network unavailable",
                    "owner": "ops",
                    "next": "restore service",
                    "n": 41,
                }
            ],
            "timeline": [{"when": "2026-07-29", "who": "owner", "what": "Started"}],
        },
    }
    path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    return path


def _receipt_payloads(project: str, legacy: dict) -> dict[tuple[str, str], dict]:
    state = _apply_migration_composed_derivations(legacy)
    payloads = {}
    for sprint in state.get("sprints", []):
        record = deepcopy(sprint)
        for item in record.get("items", []):
            if isinstance(item, dict):
                item.pop("status", None)
                item.pop("impl", None)
        payloads[("sprint", record["id"])] = {
            **record,
            "type": "sprint",
            "version": 0,
        }
    for milestone in state.get("milestones", []):
        record = deepcopy(milestone)
        payloads[("milestone", record["id"])] = {
            **record,
            "type": "milestone",
            "version": 0,
        }
    for blocker in state.get("blockers", []):
        record = deepcopy(blocker)
        record.pop("n", None)
        payloads[("blocker", record["id"])] = {
            **record,
            "type": "blocker",
            "version": 0,
        }
    payloads[("timeline", "timeline")] = {
        "id": "timeline",
        "type": "timeline",
        "version": 0,
        "events": deepcopy(state.get("timeline", [])),
    }
    payloads[("project", "project")] = _identity_manifest(project, state)
    return payloads


def _write_distributed_receipt(docs: Path, index: Path) -> dict:
    source_bytes = index.read_bytes()
    envelope = json.loads(source_bytes)
    legacy = envelope["data"]
    source_sha = _sha256_path(index)
    snapshot = (
        docs
        / ".reckon"
        / "snapshots"
        / "project-state"
        / source_sha
        / "index.json"
    )
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(source_bytes)

    rows = []
    for (resource_type, resource_id), payload in sorted(
        _receipt_payloads("sample", legacy).items()
    ):
        path = _write_staged_resource(
            docs, "sample", resource_type, resource_id, payload
        )
        rows.append(
            {
                "type": resource_type,
                "id": resource_id,
                "path": str(path.relative_to(docs)),
                "sha256": _sha256_path(path),
                "version": int(payload.get("version", 0)),
            }
        )

    marker = {
        "format": "distributed",
        "status": "complete",
        "project": "sample",
        "source": str(index.relative_to(docs)),
        "source_sha256": source_sha,
        "source_version": int(legacy.get("_version", 0)),
        "snapshot": str(snapshot.relative_to(docs)),
        "snapshot_sha256": _sha256_path(snapshot),
        "resources": rows,
        "completed_at": "2026-08-24T00:00:00+00:00",
    }
    target = marker_path(docs)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return marker


@pytest.fixture()
def migrated(tmp_path: Path) -> tuple[Path, Path, dict]:
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(docs, "alpha", "active", 0.4)
    index = _legacy_index(docs)
    original = index.read_bytes()
    result = _write_distributed_receipt(docs, index)
    return docs, index, {"original": original, "result": result}


def test_composition_orders_numbered_sprints_naturally(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    create_project_state(docs, "sample")
    for number in range(1, 13):
        write_resource(
            docs,
            "sample",
            "sprint",
            f"S{number}",
            {"theme": f"Iteration {number}", "items": []},
            0,
            create=True,
        )

    assert [
        sprint["id"] for sprint in compose_project_state(docs, "sample")["sprints"]
    ] == [f"S{number}" for number in range(1, 13)]


def test_composition_retains_nonlexicographic_marker_sprint_sequence(migrated):
    docs, _, _ = migrated
    marker = json.loads(marker_path(docs).read_text(encoding="utf-8"))
    sprint_rows = [row for row in marker["resources"] if row["type"] == "sprint"]
    other_rows = [row for row in marker["resources"] if row["type"] != "sprint"]
    marker["resources"] = [*reversed(sprint_rows), *other_rows]
    marker_path(docs).write_text(json.dumps(marker), encoding="utf-8")

    assert [
        sprint["id"] for sprint in compose_project_state(docs, "sample")["sprints"]
    ] == ["earlier", "current"]


def test_composed_parity_still_reports_authored_blocker_change(migrated):
    docs, index, _ = migrated
    legacy = json.loads(index.read_text(encoding="utf-8"))["data"]
    composed = compose_project_state(docs, "sample")
    composed["blockers"][0]["summary"] = "Different authored summary"

    old_projection = _parity_projection(legacy, "sample")
    new_projection = _parity_projection(composed, "sample")

    assert old_projection != new_projection
    assert old_projection["blockers"][0]["summary"] == "Network unavailable"
    assert new_projection["blockers"][0]["summary"] == "Different authored summary"


def test_legacy_item_lifecycle_reads_from_plan_with_compatibility_warning(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(docs, "alpha", "active", 0.4)
    index = _legacy_index(docs)
    original = index.read_bytes()

    composed = compose_project_state(docs, "sample")
    current = next(item for item in composed["sprints"] if item["id"] == "current")

    assert current["items"][0]["status"] == "active"
    assert current["items"][0]["impl"] == pytest.approx(0.4)
    assert any(
        "sprint current item alpha: persisted status is ignored" in warning
        for warning in composed["compatibility_warnings"]
    )
    assert index.read_bytes() == original


def test_typed_sprint_legacy_status_reads_derived_but_is_rejected_on_write(migrated):
    docs, _, _ = migrated
    path = resource_path(docs, "sample", "sprint", "current")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            '"slug":"alpha"',
            '"impl":0.0,"slug":"alpha","status":"pending"',
            1,
        ),
        encoding="utf-8",
    )

    sprint, version = read_resource(docs, "sample", "sprint", "current")

    assert sprint["items"][0]["status"] == "pending"
    assert any(
        "sprint current item alpha: persisted status is ignored" in warning
        for warning in sprint["compatibility_warnings"]
    )
    composed = compose_project_state(docs, "sample")
    current = next(item for item in composed["sprints"] if item["id"] == "current")
    assert current["items"][0]["status"] == "active"
    assert current["items"][0]["impl"] == pytest.approx(0.4)
    with pytest.raises(
        ValueError,
        match="sprint item 'alpha' must not persist status",
    ):
        stored = {
            **sprint,
            "compatibility_warnings": [],
            "items": [
                {
                    key: value
                    for key, value in sprint["items"][0].items()
                    if key != "impl"
                }
            ],
        }
        write_resource(
            docs,
            "sample",
            "sprint",
            "current",
            stored,
            version,
        )


def test_project_scope_routes_are_validated_and_composed(migrated):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")
    scope = {
        "owns": ["runtime orchestration"],
        "excludes": ["language vocabulary"],
        "routes": [{"work": "vocabulary", "project": "language"}],
    }

    write_resource(
        docs,
        "sample",
        "project",
        "project",
        {**project, "scope": scope},
        version,
    )

    assert compose_project_state(docs, "sample")["projects"][0]["scope"] == scope


def test_project_scope_rejects_malformed_routes(migrated):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")

    with pytest.raises(ValueError, match="route project"):
        write_resource(
            docs,
            "sample",
            "project",
            "project",
            {
                **project,
                "scope": {
                    "owns": [],
                    "routes": [{"work": "vocabulary", "project": "../other"}],
                },
            },
            version,
        )


def test_project_derivations_round_trip_beside_scope(migrated):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")
    scope = {
        "owns": ["runtime orchestration"],
        "excludes": ["language vocabulary"],
        "routes": [{"work": "vocabulary", "project": "language"}],
    }
    derivations = {
        "reckon/schema/flight.yaml": [
            "reckon/_flight_schema.py",
            "docs/_shared/flight.schema.json",
        ]
    }

    new_version = write_resource(
        docs,
        "sample",
        "project",
        "project",
        {**project, "scope": scope, "derivations": derivations},
        version,
    )

    stored, stored_version = read_resource(docs, "sample", "project", "project")
    assert new_version == stored_version == version + 1
    assert stored["derivations"] == derivations
    assert stored["scope"] == scope
    composed_project = compose_project_state(docs, "sample")["projects"][0]
    assert composed_project["derivations"] == derivations
    assert composed_project["scope"] == scope


@pytest.mark.parametrize(
    ("derivations", "offending"),
    [
        ({"/schema/source.yaml": ["generated.py"]}, "/schema/source.yaml"),
        ({"../schema/source.yaml": ["generated.py"]}, "../schema/source.yaml"),
        ({"schema/source.yaml": ["/generated.py"]}, "/generated.py"),
        ({"schema/source.yaml": ["../../generated.py"]}, "../../generated.py"),
    ],
)
def test_project_derivations_reject_paths_outside_repository(
    migrated, derivations, offending
):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")

    with pytest.raises(ValueError, match=re.escape(offending)):
        write_resource(
            docs,
            "sample",
            "project",
            "project",
            {**project, "derivations": derivations},
            version,
        )


def test_project_absent_and_empty_derivations_mean_none(migrated):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")

    assert "derivations" not in project
    assert "derivations" not in compose_project_state(docs, "sample")["projects"][0]

    write_resource(
        docs,
        "sample",
        "project",
        "project",
        {**project, "derivations": {}},
        version,
    )
    stored, _ = read_resource(docs, "sample", "project", "project")
    assert stored["derivations"] == {}
    assert compose_project_state(docs, "sample")["projects"][0]["derivations"] == {}


def test_project_north_stars_round_trip_and_compose(migrated):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")
    north_stars = [
        {
            "id": "reliable-delivery",
            "name": "Reliable delivery",
            "statement": "Every accepted change is reproducible and observable.",
            "href": "/sample/research/delivery-strategy",
        },
        {
            "id": "clear-direction",
            "name": "Clear direction",
            "statement": "Every material plan states what durable outcome it serves.",
        },
    ]

    new_version = write_resource(
        docs,
        "sample",
        "project",
        "project",
        {**project, "north_stars": north_stars},
        version,
    )

    stored, stored_version = read_resource(docs, "sample", "project", "project")
    assert new_version == stored_version == version + 1
    assert stored["north_stars"] == north_stars
    assert compose_project_state(docs, "sample")["north_stars"] == north_stars


@pytest.mark.parametrize("missing", ["id", "name", "statement"])
def test_project_north_star_requires_identity_name_and_statement(migrated, missing):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")
    north_star = {
        "id": "reliable-delivery",
        "name": "Reliable delivery",
        "statement": "Every accepted change is reproducible and observable.",
    }
    north_star.pop(missing)

    with pytest.raises(ValueError, match=missing):
        write_resource(
            docs,
            "sample",
            "project",
            "project",
            {**project, "north_stars": [north_star]},
            version,
        )


def test_project_north_star_advisory_cap_warns_without_blocking_write(migrated):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")
    north_stars = [
        {
            "id": f"direction-{index}",
            "name": f"Direction {index}",
            "statement": f"Winning outcome {index} remains visible.",
        }
        for index in range(6)
    ]

    new_version = write_resource(
        docs,
        "sample",
        "project",
        "project",
        {**project, "north_stars": north_stars},
        version,
    )

    assert new_version == version + 1
    assert (
        len(read_resource(docs, "sample", "project", "project")[0]["north_stars"]) == 6
    )
    assert audit_project_state(docs, "sample") == [
        {
            "code": "north-star-advisory-cap-exceeded",
            "severity": "warning",
            "message": "project declares 6 north-stars; the advisory cap is 5",
        }
    ]


def test_project_without_north_stars_reads_unchanged(migrated):
    docs, _index, _evidence = migrated
    project, version = read_resource(docs, "sample", "project", "project")

    assert "north_stars" not in project
    assert "north_stars" not in compose_project_state(docs, "sample")
    assert read_resource(docs, "sample", "project", "project") == (
        project,
        version,
    )


def test_distributed_resource_access_rejects_before_activation(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(docs, "alpha", "active", 0.4)
    _legacy_index(docs)

    with pytest.raises(ProjectStateError, match="distributed_resource_inactive"):
        read_resource(docs, "sample", "sprint", "current")
    with pytest.raises(ProjectStateError, match="distributed_resource_inactive"):
        write_resource(
            docs,
            "sample",
            "sprint",
            "current",
            {"theme": "Current", "status": "active", "items": []},
            0,
            create=True,
        )

    checkout = str(docs.parent)
    read_result = mcp_module._read_plan(
        "sample", "current", doc_type="sprint", checkout_path=checkout
    )
    assert read_result["error"] == "project_state_error"
    assert "distributed_resource_inactive" in read_result["detail"]
    write_result = mcp_module._edit_plan(
        "sample",
        "future",
        [{"op": "set", "path": "status", "value": "planned"}],
        0,
        create=True,
        checkout_path=checkout,
        doc_type="sprint",
    )
    assert write_result["error"] == "project_state_error"
    assert "distributed_resource_inactive" in write_result["detail"]

def test_independent_versions_and_same_resource_conflict(migrated):
    docs, _, _ = migrated
    sprint, sprint_version = read_resource(docs, "sample", "sprint", "current")
    timeline, timeline_version = read_resource(docs, "sample", "timeline", "timeline")

    new_timeline_version = append_timeline_event(
        docs,
        "sample",
        {"when": "2026-07-30", "who": "owner", "what": "Continued"},
        timeline_version,
    )
    assert new_timeline_version == timeline_version + 1
    assert read_resource(docs, "sample", "sprint", "current")[1] == sprint_version

    new_sprint_version = write_resource(
        docs,
        "sample",
        "sprint",
        "current",
        {**sprint, "summary": "updated"},
        sprint_version,
    )
    assert new_sprint_version == sprint_version + 1
    with pytest.raises(ProjectStateConflict):
        write_resource(
            docs,
            "sample",
            "sprint",
            "current",
            {**sprint, "summary": "stale"},
            sprint_version,
        )
    assert timeline["events"][0]["what"] == "Started"


def test_concurrent_same_resource_writers_have_one_winner(migrated):
    docs, _, _ = migrated
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    workers = [
        context.Process(
            target=_concurrent_write,
            args=(str(docs), barrier, queue, summary),
        )
        for summary in ("first", "second")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    results = sorted(queue.get(timeout=2)[0] for _ in workers)
    assert results == ["conflict", "ok"]


def test_default_sprint_focus_is_earliest_incomplete_and_never_persisted(migrated):
    docs, _, _ = migrated
    _write_plan(docs, "beta", "active", 0.2)
    earlier, earlier_version = read_resource(docs, "sample", "sprint", "earlier")
    write_resource(
        docs,
        "sample",
        "sprint",
        "earlier",
        {
            **earlier,
            "status": "active",
            "items": [{"slug": "beta"}],
        },
        earlier_version,
    )

    composed = compose_project_state(docs, "sample")
    incomplete = [
        sprint["id"]
        for sprint in composed["sprints"]
        if any(item["status"] not in {"shipped", "done"} for item in sprint["items"])
    ]
    assert incomplete == ["current", "earlier"]
    assert composed["active_sprint_id"] == "current"
    assert audit_project_state(docs, "sample") == []

    sprint_paths = [
        resource_path(docs, "sample", "sprint", sprint_id) for sprint_id in incomplete
    ]
    sprint_bytes = {path: path.read_bytes() for path in sprint_paths}
    _write_plan(docs, "alpha", "shipped", 1.0)

    refreshed = compose_project_state(docs, "sample")
    assert refreshed["active_sprint_id"] == "earlier"
    assert {path: path.read_bytes() for path in sprint_paths} == sprint_bytes


def test_concurrent_sprint_status_updates_both_succeed(migrated):
    docs, _, _ = migrated
    current, current_version = read_resource(docs, "sample", "sprint", "current")
    write_resource(
        docs,
        "sample",
        "sprint",
        "current",
        {**current, "status": "planned"},
        current_version,
    )
    write_resource(
        docs,
        "sample",
        "sprint",
        "future",
        {"theme": "Future", "status": "planned", "items": []},
        0,
        create=True,
    )

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    workers = [
        context.Process(
            target=_concurrent_status_update,
            args=(str(docs), sprint_id, barrier, queue),
        )
        for sprint_id in ("earlier", "future")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    results = [queue.get(timeout=2) for _ in workers]
    assert sorted(result[0] for result in results) == ["ok", "ok"]
    active = [
        sprint["id"]
        for sprint in compose_project_state(docs, "sample")["sprints"]
        if sprint["status"] == "active"
    ]
    assert set(active) == {"earlier", "future"}


def test_sprint_edits_do_not_create_global_focus_state(migrated):
    docs, _, _ = migrated
    earlier, version = read_resource(docs, "sample", "sprint", "earlier")
    write_resource(
        docs,
        "sample",
        "sprint",
        "earlier",
        {**earlier, "status": "active"},
        version,
    )
    assert not (
        docs / ".reckon" / "locks" / "sample-invariant-active-sprint.lock"
    ).exists()
    project, _ = read_resource(docs, "sample", "project", "project")
    assert "active_sprint_id" not in project


def test_timeline_is_append_only(migrated):
    docs, _, _ = migrated
    timeline, version = read_resource(docs, "sample", "timeline", "timeline")
    with pytest.raises(ValueError, match="already exists"):
        append_timeline_event(
            docs,
            "sample",
            timeline["events"][0],
            version,
        )


def test_direct_timeline_write_requires_exact_history_prefix(migrated):
    docs, _, _ = migrated
    timeline, version = read_resource(docs, "sample", "timeline", "timeline")
    original = list(timeline["events"])
    for replacement in (
        [],
        [{**original[0], "what": "rewritten"}],
        [original[0], {**original[0], "id": "event-second"}][::-1],
    ):
        with pytest.raises(ValueError, match="append-only"):
            write_resource(
                docs,
                "sample",
                "timeline",
                "timeline",
                {**timeline, "events": replacement},
                version,
            )
    assert (
        read_resource(docs, "sample", "timeline", "timeline")[0]["events"] == original
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"status": "invented"}, "sprint status"),
        ({"items": [{"slug": "../escape"}]}, "single safe path segment"),
        (
            {"items": [{"slug": "alpha"}, {"slug": "alpha"}]},
            "duplicate sprint item",
        ),
        ({"items": [{"slug": "missing"}]}, "does not resolve to a live plan"),
        (
            {"items": [{"slug": "alpha", "blocked_by": ["missing"]}]},
            "missing blockers",
        ),
        (
            {"items": [{"slug": "alpha", "milestone": "missing"}]},
            "missing milestone",
        ),
        ({"items": [{"slug": "alpha", "status": "active"}]}, "must not persist"),
        ({"items": [{"slug": "alpha", "impl": 0.5}]}, "must not persist"),
    ],
)
def test_sprint_writes_strictly_validate_identity_and_references(
    migrated, mutation, message
):
    docs, _, _ = migrated
    sprint, version = read_resource(docs, "sample", "sprint", "earlier")
    with pytest.raises(ValueError, match=message):
        write_resource(
            docs,
            "sample",
            "sprint",
            "earlier",
            {**sprint, **mutation},
            version,
        )


def test_cross_sprint_move_recovers_after_second_write_failure(migrated):
    docs, _, _ = migrated
    source_before = resource_path(docs, "sample", "sprint", "current").read_bytes()
    _, from_version = read_resource(docs, "sample", "sprint", "current")
    _, to_version = read_resource(docs, "sample", "sprint", "earlier")

    def fail() -> None:
        raise OSError("second write failed")

    with pytest.raises(OSError, match="second write"):
        move_sprint_item(
            docs,
            "sample",
            "alpha",
            "current",
            "earlier",
            from_version,
            to_version,
            after_first_write=fail,
        )
    assert (
        resource_path(docs, "sample", "sprint", "current").read_bytes() == source_before
    )
    assert read_resource(docs, "sample", "sprint", "earlier")[0]["items"] == []


def test_cross_sprint_move_journal_recovers_after_interruption(migrated):
    docs, _, _ = migrated
    source_before = resource_path(docs, "sample", "sprint", "current").read_bytes()
    target_before = resource_path(docs, "sample", "sprint", "earlier").read_bytes()
    _, from_version = read_resource(docs, "sample", "sprint", "current")
    _, to_version = read_resource(docs, "sample", "sprint", "earlier")

    def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        move_sprint_item(
            docs,
            "sample",
            "alpha",
            "current",
            "earlier",
            from_version,
            to_version,
            after_first_write=interrupt,
        )
    journals = list((docs / ".reckon" / "transactions").glob("*.json"))
    assert len(journals) == 1

    # The next read performs deterministic journal recovery before returning.
    read_resource(docs, "sample", "sprint", "current")
    assert not journals[0].exists()
    assert (
        resource_path(docs, "sample", "sprint", "current").read_bytes() == source_before
    )
    assert (
        resource_path(docs, "sample", "sprint", "earlier").read_bytes() == target_before
    )


def test_concurrent_recovery_readers_restore_once_without_errors(migrated):
    docs, _, _ = migrated
    _, from_version = read_resource(docs, "sample", "sprint", "current")
    _, to_version = read_resource(docs, "sample", "sprint", "earlier")

    def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        move_sprint_item(
            docs,
            "sample",
            "alpha",
            "current",
            "earlier",
            from_version,
            to_version,
            after_first_write=interrupt,
        )
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    workers = [
        context.Process(target=_concurrent_read, args=(str(docs), barrier, queue))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    results = [queue.get(timeout=2) for _ in workers]
    assert all(result[0] == "ok" for result in results)
    assert not list((docs / ".reckon" / "transactions").glob("*.json"))
    source, _ = read_resource(docs, "sample", "sprint", "current")
    target, _ = read_resource(docs, "sample", "sprint", "earlier")
    assert [item["slug"] for item in source["items"]] == ["alpha"]
    assert target["items"] == []


def test_committed_move_journal_is_removed_without_rollback(migrated):
    docs, _, _ = migrated
    sprint, version = read_resource(docs, "sample", "sprint", "current")
    original = resource_path(docs, "sample", "sprint", "current").read_bytes()
    write_resource(
        docs,
        "sample",
        "sprint",
        "current",
        {**sprint, "summary": "committed value"},
        version,
    )
    journal = docs / ".reckon" / "transactions" / "sprint-move-committed.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "kind": "sprint-item-move",
                "status": "committed",
                "project": "sample",
                "source_id": "current",
                "target_id": "earlier",
                "source_before": base64.b64encode(original).decode(),
                "target_before": base64.b64encode(
                    resource_path(docs, "sample", "sprint", "earlier").read_bytes()
                ).decode(),
            }
        ),
        encoding="utf-8",
    )

    current, _ = read_resource(docs, "sample", "sprint", "current")
    assert current["summary"] == "committed value"
    assert not journal.exists()


def test_move_journal_never_trusts_repository_paths(migrated, tmp_path):
    docs, _, _ = migrated
    external = tmp_path / "outside.txt"
    external.write_text("untouched", encoding="utf-8")
    journal = docs / ".reckon" / "transactions" / "sprint-move-malicious.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "kind": "sprint-item-move",
                "status": "prepared",
                "project": "sample",
                "source_id": "../../outside",
                "target_id": "earlier",
                "source_path": str(external),
                "target_path": str(external),
                "source_before": base64.b64encode(b"changed").decode(),
                "target_before": base64.b64encode(b"changed").decode(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProjectStateError, match="malformed transaction journal"):
        read_resource(docs, "sample", "sprint", "current")
    assert external.read_text(encoding="utf-8") == "untouched"


def test_compat_index_read_and_write_rejection(migrated):
    docs, _, _ = migrated
    data, version = read_plan("sample", "index", root=docs.parent)
    assert version == 0
    assert data["source_format"] == "distributed"
    assert "sprint:current" in data["resource_versions"]
    with pytest.raises(LegacyIndexReadOnly, match="legacy_index_read_only"):
        write_plan("sample", "index", data, version, root=docs.parent)


def test_mcp_typed_resource_read_write_and_legacy_guidance(migrated):
    docs, _, _ = migrated
    checkout = str(docs.parent)
    sprint = mcp_module._read_plan(
        "sample", "current", doc_type="sprint", checkout_path=checkout
    )
    assert sprint["data"]["theme"] == "Current"

    edited = mcp_module._edit_plan(
        "sample",
        "current",
        [{"op": "set", "path": "summary", "value": "Composed"}],
        sprint["version"],
        checkout_path=checkout,
        doc_type="sprint",
    )
    assert edited["ok"] is True
    assert edited["new_version"] == sprint["version"] + 1

    aggregate = mcp_module._read_plan("sample", "index", checkout_path=checkout)
    rejected = mcp_module._edit_plan(
        "sample",
        "index",
        [{"op": "set", "path": "active_sprint_id", "value": None}],
        aggregate["version"],
        checkout_path=checkout,
    )
    assert rejected["error"] == "legacy_index_read_only"
    assert "named resource" in rejected["hint"]


def test_mcp_creates_new_typed_resource_and_discovery_composes_it(migrated):
    docs, _, _ = migrated
    checkout = str(docs.parent)
    created = mcp_module._edit_plan(
        "sample",
        "review",
        [
            {"op": "set", "path": "name", "value": "Review"},
            {"op": "set", "path": "status", "value": "planned"},
        ],
        0,
        create=True,
        checkout_path=checkout,
        doc_type="milestone",
    )
    assert created["ok"] is True
    discovered = mcp_module._read_plan("sample", checkout_path=checkout)
    assert any(item["id"] == "review" for item in discovered["milestones"])


def test_static_build_writes_projection_without_touching_canonical_index(migrated):
    docs, index, evidence = migrated
    result = CliRunner().invoke(
        cli_module.main, ["build", str(docs), "--project", "sample"]
    )
    assert result.exit_code == 0, result.output
    assert index.read_bytes() == evidence["original"]
    projection = docs / "state" / "sample" / "projection.json"
    data = json.loads(projection.read_text())["data"]
    assert data["source_format"] == "distributed"
    assert data["active_sprint_id"] == "current"
    assert "alpha" in {item["slug"] for item in data["inventory"]}


def test_sync_preserves_frozen_index(migrated, tmp_path):
    docs, index, evidence = migrated
    result = CliRunner().invoke(
        cli_module.main,
        [
            "sync",
            str(docs),
            "--project",
            "sample",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "state"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "preserved frozen index.json" in result.output
    assert index.read_bytes() == evidence["original"]


def test_missing_distributed_resource_never_falls_back(migrated):
    docs, _, _ = migrated
    resource_path(docs, "sample", "sprint", "current").unlink()
    with pytest.raises(ProjectStateError, match="missing"):
        compose_project_state(docs, "sample")
    result = mcp_module._read_plan("sample", checkout_path=str(docs.parent))
    assert result["error"] == "project_state_error"
    assert "missing" in result["detail"]


def test_legacy_discovery_ignores_partial_distributed_destinations(tmp_path):
    from reckon.serve import discover_plans

    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(docs, "alpha", "active", 0.4)
    _legacy_index(docs)
    partial = resource_path(docs, "sample", "sprint", "partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text(
        "<html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="sprint">'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    discovered = discover_plans(docs, "sample", docs / "state")
    assert discovered["source_format"] == "legacy-index"
    assert {row["id"] for row in discovered["sprints"]} == {"earlier", "current"}


def test_legacy_discovery_uses_index_when_project_json_diverges(tmp_path):
    from reckon.serve import discover_plans

    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(docs, "alpha", "active", 0.4)
    _legacy_index(docs)
    project_json = docs / "state" / "sample" / "project.json"
    project_json.write_text(
        json.dumps(
            {
                "project": "sample",
                "doc": "project",
                "data": {
                    "sprints": [
                        {
                            "id": "wrong",
                            "status": "active",
                            "items": [],
                        }
                    ],
                    "milestones": [{"id": "wrong"}],
                },
            }
        ),
        encoding="utf-8",
    )

    discovered = discover_plans(docs, "sample", docs / "state")
    assert discovered["source_format"] == "legacy-index"
    assert {row["id"] for row in discovered["sprints"]} == {
        "earlier",
        "current",
    }
    assert {row["id"] for row in discovered["milestones"]} == {"launch"}


def test_http_project_resources_are_symmetric_and_index_is_frozen(
    migrated, tmp_path, monkeypatch
):
    import reckon.serve as serve_module

    docs, index, evidence = migrated
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"sample": str(docs)}), encoding="utf-8")
    monkeypatch.setattr(serve_module, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(serve_module, "_STATE_ROOT", docs / "state")
    serve_module._DISC_CACHE.clear()
    server = serve_module.ThreadingHTTPServer(("127.0.0.1", 0), serve_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request("GET", "/plan/sample/sprints/current")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["data"]["theme"] == "Current"
        version = payload["version"]

        connection.request(
            "POST",
            "/plan/sample/sprints/current",
            body=json.dumps({"summary": "HTTP updated"}),
            headers={"Content-Type": "application/json", "If-Match": str(version)},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["version"] == version + 1

        connection.request(
            "POST",
            "/state/sample/index",
            body=json.dumps({"active_sprint_id": None}),
            headers={"Content-Type": "application/json", "If-Match": "8"},
        )
        response = connection.getresponse()
        assert response.status == 409
        assert json.loads(response.read())["error"] == "legacy_index_read_only"
        assert index.read_bytes() == evidence["original"]

        resource_path(docs, "sample", "sprint", "current").unlink()
        serve_module._DISC_CACHE.clear()
        connection.request("GET", "/state/sample/index.json")
        response = connection.getresponse()
        assert response.status == 500
        assert json.loads(response.read())["error"] == (
            "distributed_project_state_invalid"
        )
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_state_loader(discovery_status: int) -> dict:
    loader = Path("docs/ui/state-loader.js").resolve()
    script = f"""
const fs = require("fs");
global.window = {{location: {{pathname: "/sample/"}}}};
global.document = {{
  querySelector: () => ({{content: "sample"}})
}};
const projection = {{
  data: {{
    source_format: "distributed",
    inventory: [{{slug: "alpha", type: "plan", status: "active"}}],
    sprints: [],
    milestones: []
  }}
}};
global.fetch = async (url) => {{
  if (url === "state/sample/projection.json") {{
    return {{ok: true, status: 200, json: async () => projection}};
  }}
  if (url === "/_discover/sample") {{
    return {{ok: false, status: {discovery_status}, json: async () => ({{}})}};
  }}
  throw new Error("unexpected fetch " + url);
}};
eval(fs.readFileSync({json.dumps(str(loader))}, "utf8"));
window.STATE_READY.then(
  () => console.log(JSON.stringify({{
    resolved: true,
    inventory: window.STATE.inventory.map(item => item.slug)
  }})),
  error => console.log(JSON.stringify({{
    resolved: false,
    message: error.message,
    state_error: window.STATE_ERROR && window.STATE_ERROR.message,
    pending_view: window.projectStateLoadView(
      null,
      window.STATE_LOAD.startedAt + 3200
    ),
    error_view: window.projectStateLoadView(error)
  }}))
);
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _render_project_state_load_panel(load: dict) -> dict:
    shell = Path("docs/ui/shell.jsx").resolve()
    source = shell.read_text(encoding="utf-8")
    start = source.index("function ProjectStateLoadPanel")
    end = source.index("\nfunction ReadyGate", start)
    component = source[start:end]
    script = f"""
const React = {{
  createElement: (type, props, ...children) => ({{
    type,
    props: props || {{}},
    children
  }})
}};
{component}
const tree = ProjectStateLoadPanel({{load: {json.dumps(load)}}});
function textContent(node) {{
  if (node === null || node === undefined || node === false) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  return (node.children || []).map(textContent).join(" ");
}}
console.log(JSON.stringify({{role: tree.props.role, text: textContent(tree)}}));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_spa_static_projection_allows_explicit_discovery_not_found():
    result = _run_state_loader(404)
    assert result == {"resolved": True, "inventory": ["alpha"]}


def test_spa_projection_does_not_hide_discovery_server_failure():
    result = _run_state_loader(500)
    assert result["resolved"] is False
    assert result["message"] == "/_discover/sample returned HTTP 500"
    assert result["state_error"] == result["message"]


def test_spa_discovery_failure_panel_replaces_pending_status():
    result = _run_state_loader(503)
    pending = _render_project_state_load_panel(result["pending_view"])
    failed = _render_project_state_load_panel(result["error_view"])

    assert pending["role"] == "status"
    assert "Loading plan state" in pending["text"]
    assert "3s elapsed" in pending["text"]
    assert failed["role"] == "alert"
    assert "/_discover/sample" in failed["text"]
    assert "HTTP 503" in failed["text"]
    assert "Loading plan state" not in failed["text"]
