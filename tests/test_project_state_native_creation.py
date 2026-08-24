"""Native distributed project-state creation through sync."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.project_state import marker_path, project_state_mode


def _sync(docs: Path, tmp_path: Path):
    return CliRunner().invoke(
        cli_module.main,
        [
            "sync",
            str(docs),
            "--project",
            "sample",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "config-state"),
        ],
    )


def test_sync_creates_distributed_state_without_legacy_index(tmp_path: Path):
    docs = tmp_path / "checkout" / "docs"
    docs.mkdir(parents=True)
    state_dir = docs / "state" / "sample"
    assert not state_dir.exists()

    result = _sync(docs, tmp_path)

    assert result.exit_code == 0, result.output
    assert "created distributed project state (resources=2)" in result.output
    mode = project_state_mode(docs)
    assert mode.format == "distributed"
    assert not (state_dir / "index.json").exists()
    marker = mode.marker or {}
    assert marker["status"] == "complete"
    assert {(row["type"], row["id"]) for row in marker["resources"]} == {
        ("project", "project"),
        ("timeline", "timeline"),
    }
    for migration_only_field in (
        "source",
        "source_sha256",
        "source_version",
        "snapshot",
        "snapshot_sha256",
        "destination_snapshot_manifest",
        "parity_sha256",
    ):
        assert migration_only_field not in marker


def test_sync_preserves_existing_legacy_index_without_conversion(tmp_path: Path):
    docs = tmp_path / "checkout" / "docs"
    state_dir = docs / "state" / "sample"
    state_dir.mkdir(parents=True)
    index = state_dir / "index.json"
    original = json.dumps(
        {
            "updated": "2026-08-24T00:00:00",
            "project": "sample",
            "doc": "index",
            "data": {"_version": 7, "sprints": []},
        },
        indent=2,
    ).encode()
    index.write_bytes(original)

    result = _sync(docs, tmp_path)

    assert result.exit_code == 0, result.output
    assert "preserved existing legacy index.json" in result.output
    assert project_state_mode(docs).format == "legacy"
    assert index.read_bytes() == original
    assert not marker_path(docs).exists()
