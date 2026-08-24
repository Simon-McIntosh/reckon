"""Fleet discovery, snapshots, transactional migration, and rollback."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html
from reckon.cli import main
from reckon.fleet_migration import (
    create_snapshot,
    discover_registry,
    enrich_ledger_inventories,
    migrate_repository,
    preflight_repository,
    record_repository_commit,
    rollback_repository,
    run_fleet_migration,
)
from reckon.project_state import project_state_mode, read_resource
from reckon.resources import resolve_resource


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path, name: str) -> tuple[Path, Path]:
    repo = tmp_path / name
    docs = repo / "docs"
    docs.mkdir(parents=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{name}">'
        "<title>Work</title>"
        '<link rel="stylesheet" href="/_shared/foundation.css">'
        '<link rel="stylesheet" href="/_shared/dashboard.css">'
        "</head><body><main>"
        "<h1>Work</h1><p>Migration fixture.</p>"
        "</main></body></html>"
    )
    state = {
        "project": name,
        "type": "plan",
        "slug": "work",
        "title": "Work",
        "summary": "Migrate this work record.",
        "status": "active",
        "roi": "high",
        "effort": "S",
        "impl": 0.2,
        "tier": "sonnet",
        "version": 0,
        "followups": [
            {
                "id": "next-work",
                "status": "open",
                "tier": "haiku",
                "written_by": "owner",
                "written_at": "2026-07-29",
                "title": "Continue work",
                "body": "<p>Continue.</p>",
                "prompt": (
                    f"Project: {name}\n"
                    "Plan: work\n"
                    "Context\n  Continue.\n"
                    "Done-when\n  1. complete\n"
                ),
            }
        ],
    }
    (docs / "work.html").write_text(_plan_html.write_state(bare, state))
    index = {
        "updated": "2026-07-29T00:00:00",
        "project": name,
        "doc": "index",
        "data": {
            "_version": 1,
            "active_sprint_id": "current",
            "projects": [
                {
                    "project": name,
                    "owner": "owner",
                    "published": f"example.invalid/{name}",
                }
            ],
            "sprints": [
                {
                    "id": "current",
                    "theme": "Current",
                    "status": "active",
                    "items": [
                        {
                            "slug": "work",
                            "status": "active",
                            "impl": 0.2,
                            "tier": "sonnet",
                            "why_now": "now",
                        }
                    ],
                }
            ],
            "milestones": [],
            "blockers": [],
            "timeline": [],
        },
    }
    state_dir = docs / "state" / name
    state_dir.mkdir(parents=True)
    (state_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "owner@example.invalid")
    _git(repo, "config", "user.name", "Owner")
    _git(repo, "add", "docs/work.html", f"docs/state/{name}/index.json")
    _git(repo, "commit", "-m", "docs: add planning state")
    return repo, docs


def test_discovery_uses_runtime_registry_without_project_allowlist(tmp_path):
    _, first = _repository(tmp_path, "first")
    _, second = _repository(tmp_path, "second")
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"second": str(second), "first": str(first)}))

    result = discover_registry(mounts)

    assert result["path"] == str(mounts.resolve())
    assert [row["project"] for row in result["projects"]] == ["first", "second"]
    assert len(result["sha256"]) == 64


def test_fleet_run_snapshots_every_mount_and_applies_only_selected_project(tmp_path):
    first_repo, first_docs = _repository(tmp_path, "first")
    _, second_docs = _repository(tmp_path, "second")
    mounts = tmp_path / "mounts.json"
    mounts.write_text(
        json.dumps({"first": str(first_docs), "second": str(second_docs)})
    )
    output = tmp_path / "migration-output"

    ledger = run_fleet_migration(
        mounts_path=mounts,
        output_dir=output,
        run_id="reviewed-run",
        apply_projects=["first"],
    )

    rows = {row["project"]: row for row in ledger["repositories"]}
    assert ledger["complete"] is True
    assert rows["first"]["state"] == "verified", rows["first"]
    assert rows["second"]["state"] == "deferred"
    assert "authorize this repository write scope" in rows["second"]["required_action"]
    assert Path(rows["first"]["snapshot"]["path"]).is_file()
    assert Path(rows["second"]["snapshot"]["path"]).is_file()
    assert rows["first"]["before"]["layout"] == {"typed": 0, "legacy": 1}
    assert rows["first"]["before"]["legacy_capabilities"] == 3
    assert rows["first"]["after"]["layout"] == {"typed": 2, "legacy": 0}
    assert rows["first"]["after"]["legacy_capabilities"] == 0
    assert rows["first"]["result"]["verification"]["legacy_capabilities"] == 0
    with zipfile.ZipFile(rows["second"]["snapshot"]["path"]) as archive:
        assert "manifest.json" in archive.namelist()
        assert "contents/work.html" in archive.namelist()

    migrated = resolve_resource(first_docs, "first", "work", "plan")
    assert migrated is not None
    assert migrated.relative_path.as_posix() == "plans/work.html"
    assert not (first_docs / "work.html").exists()
    state = _plan_html.read_state(migrated.path.read_text())
    assert state["effort_calibrated"] is False
    assert state["capability"]["class"] == "general"
    assert "tier" not in state
    assert state["followups"][0]["capability"]["class"] == "routine"
    assert "tier" not in state["followups"][0]
    assert project_state_mode(first_docs).format == "distributed"
    sprint, _ = read_resource(first_docs, "first", "sprint", "current")
    assert sprint["items"][0]["capability"]["class"] == "general"
    assert "tier" not in sprint["items"][0]
    assert not (first_docs / ".reckon" / "locks").exists()
    status = _git(first_repo, "status", "--short")
    assert "D docs/work.html" in status
    assert "?? docs/plans/" in status

    before_rerun = (first_docs / "plans" / "work.html").read_bytes()
    repeated = run_fleet_migration(
        mounts_path=mounts,
        output_dir=output,
        run_id="reviewed-run",
        apply_projects=["first"],
    )
    assert repeated["complete"] is True
    assert (first_docs / "plans" / "work.html").read_bytes() == before_rerun

    changed = rows["first"]["result"]["changes"]
    changed_paths = [*changed["created"], *changed["modified"], *changed["deleted"]]
    rollback_repository(
        Path(rows["first"]["snapshot"]["path"]),
        first_docs,
        changed_paths,
    )
    assert (first_docs / "work.html").is_file()
    assert not (first_docs / "plans" / "work.html").exists()
    assert project_state_mode(first_docs).format == "legacy"


def test_dirty_migration_path_is_deferred_with_exact_path(tmp_path):
    _, docs = _repository(tmp_path, "sample")
    (docs / "work.html").write_text(
        (docs / "work.html").read_text().replace("Migration fixture", "User edit")
    )

    result = preflight_repository(docs, "sample")

    assert result["ok"] is False
    blocker = next(
        item for item in result["blockers"] if item["code"] == "dirty-migration-paths"
    )
    assert blocker["paths"] == ["docs/work.html"]


def test_detached_alternate_worktree_with_no_migration_changes_is_ignored(tmp_path):
    _, docs = _repository(tmp_path, "sample")
    _git(docs.parent, "worktree", "add", "--detach", str(tmp_path / "alternate-clean"), "HEAD")
    (tmp_path / "alternate-clean" / "notes.txt").write_text("ignore")

    result = preflight_repository(docs, "sample")

    assert result["ok"] is True, result["blockers"]
    assert not any(
        item["code"] == "dirty-alternate-worktrees" for item in result["blockers"]
    )


def test_detached_alternate_worktree_with_dirty_html_blocks_preflight(tmp_path):
    _, docs = _repository(tmp_path, "sample")
    _git(docs.parent, "worktree", "add", "--detach", str(tmp_path / "alternate-html"), "HEAD")
    alternate = tmp_path / "alternate-html" / "docs" / "work.html"
    alternate.write_text((alternate.read_text()).replace("Migration fixture", "Alternate edit"))

    result = preflight_repository(docs, "sample")

    assert result["ok"] is False
    blocker = next(
        item for item in result["blockers"] if item["code"] == "dirty-alternate-worktrees"
    )
    assert blocker["worktrees"] == [
        {"path": str(tmp_path / "alternate-html"), "paths": ["docs/work.html"]}
    ]


def test_install_failure_restores_snapshot_exactly(tmp_path):
    repo, docs = _repository(tmp_path, "sample")
    repository = preflight_repository(docs, "sample")["repository"]
    snapshot = tmp_path / "sample.zip"
    create_snapshot(
        docs,
        snapshot,
        project="sample",
        repository=repository,
        registry_sha256="a" * 64,
    )

    def fail_after_first_write(position: int, _relative: str) -> None:
        if position == 1:
            raise RuntimeError("injected installation failure")

    with pytest.raises(RuntimeError, match="injected installation failure"):
        migrate_repository(
            docs,
            "sample",
            snapshot,
            staging_parent=tmp_path / "staging",
            install_hook=fail_after_first_write,
        )

    assert _git(repo, "status", "--short") == ""
    assert (docs / "work.html").is_file()
    assert project_state_mode(docs).format == "legacy"


def test_cli_and_commit_record_keep_ledger_machine_readable(tmp_path):
    repo, docs = _repository(tmp_path, "sample")
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"sample": str(docs)}))
    output = tmp_path / "output"
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "migrate-fleet",
            "--mounts",
            str(mounts),
            "--output-dir",
            str(output),
            "--run-id",
            "cli-run",
            "--apply-project",
            "sample",
        ],
    )

    assert result.exit_code == 0, result.output
    ledger_path = output / "ledger.json"
    assert f"ledger: {ledger_path}" in result.output
    ledger = json.loads(ledger_path.read_text())
    ledger["repositories"][0].pop("before")
    ledger["repositories"][0].pop("after")
    ledger_path.write_text(json.dumps(ledger))
    enriched = enrich_ledger_inventories(ledger_path)
    assert enriched["repositories"][0]["before"]["legacy_capabilities"] == 3
    assert enriched["repositories"][0]["after"]["legacy_capabilities"] == 0
    row = record_repository_commit(
        ledger_path,
        "sample",
        _git(repo, "rev-parse", "HEAD"),
        "origin/main",
    )
    assert row["output_commit"]
    assert row["push_ref"] == "origin/main"
