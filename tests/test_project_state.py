"""Distributed project-state storage, migration, and concurrency tests."""

from __future__ import annotations

import json
import multiprocessing
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
    append_timeline_event,
    compose_project_state,
    migrate_project_state,
    move_sprint_item,
    project_state_mode,
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


def _write_plan(docs: Path, slug: str, status: str, impl: float) -> None:
    path = docs / "plans" / f"{slug}.html"
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
            "milestones": [
                {"id": "launch", "name": "Launch", "status": "active"}
            ],
            "blockers": [
                {
                    "id": "network",
                    "summary": "Network unavailable",
                    "owner": "ops",
                    "next": "restore service",
                    "n": 41,
                }
            ],
            "timeline": [
                {"when": "2026-07-29", "who": "owner", "what": "Started"}
            ],
        },
    }
    path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def migrated(tmp_path: Path) -> tuple[Path, Path, dict]:
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(docs, "alpha", "active", 0.4)
    index = _legacy_index(docs)
    original = index.read_bytes()
    result = migrate_project_state(docs, "sample")
    return docs, index, {"original": original, "result": result}


def test_migration_preserves_index_snapshot_and_composed_parity(migrated):
    docs, index, evidence = migrated
    result = evidence["result"]

    assert project_state_mode(docs).format == "distributed"
    assert index.read_bytes() == evidence["original"]
    snapshot = docs / result["snapshot"]
    assert snapshot.read_bytes() == evidence["original"]
    assert result["source_sha256"] == result["snapshot_sha256"]

    composed = compose_project_state(docs, "sample")
    assert composed["source_format"] == "distributed"
    assert composed["active_sprint_id"] == "current"
    current = next(item for item in composed["sprints"] if item["id"] == "current")
    assert current["items"][0]["status"] == "active"
    assert current["items"][0]["impl"] == pytest.approx(0.4)
    assert composed["blockers"][0]["n"] == 1
    assert composed["timeline"][0]["id"].startswith("event-")
    manifest = composed["projects"][0]
    assert manifest["owner"] == "owner"
    assert "path" not in manifest
    assert "plans_count" not in manifest


def test_migration_rerun_verifies_and_changed_source_rejects(migrated):
    docs, index, _ = migrated
    rerun = migrate_project_state(docs, "sample")
    assert rerun["changed"] is False

    index.write_text(index.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ProjectStateError, match="changed after distributed activation"):
        migrate_project_state(docs, "sample")


def test_source_mutation_before_install_leaves_legacy_canonical(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    index = _legacy_index(docs)

    def mutate() -> None:
        index.write_text(index.read_text() + "\n", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="changed while migration was staged"):
        migrate_project_state(docs, "sample", before_install=mutate)
    assert project_state_mode(docs).format == "legacy"


def test_install_failure_leaves_legacy_mode(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _legacy_index(docs)

    def fail(position: int, source: Path, destination: Path) -> None:
        del source, destination
        if position == 1:
            raise OSError("injected install failure")

    with pytest.raises(OSError, match="injected"):
        migrate_project_state(docs, "sample", install_hook=fail)
    assert project_state_mode(docs).format == "legacy"
    assert not (docs / ".reckon" / "project-state-migration.json").exists()


def test_independent_versions_and_same_resource_conflict(migrated):
    docs, _, _ = migrated
    sprint, sprint_version = read_resource(docs, "sample", "sprint", "current")
    timeline, timeline_version = read_resource(
        docs, "sample", "timeline", "timeline"
    )

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
        worker.join(timeout=10)
        assert worker.exitcode == 0
    results = sorted(queue.get(timeout=2)[0] for _ in workers)
    assert results == ["conflict", "ok"]


def test_unique_active_is_derived_and_multiple_active_rejects(migrated):
    docs, _, _ = migrated
    earlier, earlier_version = read_resource(
        docs, "sample", "sprint", "earlier"
    )
    write_resource(
        docs,
        "sample",
        "sprint",
        "earlier",
        {**earlier, "status": "active"},
        earlier_version,
    )
    with pytest.raises(ProjectStateError, match="multiple active sprints"):
        compose_project_state(docs, "sample")


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
        resource_path(docs, "sample", "sprint", "current").read_bytes()
        == source_before
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
    assert resource_path(docs, "sample", "sprint", "current").read_bytes() == source_before
    assert resource_path(docs, "sample", "sprint", "earlier").read_bytes() == target_before


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

    aggregate = mcp_module._read_plan(
        "sample", "index", checkout_path=checkout
    )
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
