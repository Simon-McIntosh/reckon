from __future__ import annotations

import json
import os
import time
from pathlib import Path

from click.testing import CliRunner

from reckon._plan_html import write_state
from reckon.cli import main


def _write_plan(docs_dir: Path, slug: str, state: dict) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f'<title>{slug}</title></head>'
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs_dir / f"{slug}.html"
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


def _age_file(path: Path, *, days: int) -> None:
    stamp = time.time() - (days * 86400)
    os.utime(path, (stamp, stamp))


def test_audit_flags_stale_missing_impl_and_stale_rca(tmp_path, monkeypatch):
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))

    stale = _write_plan(
        docs_dir,
        "stale-plan",
        {"slug": "stale-plan", "title": "Stale Plan", "status": "active", "impl": 0.5},
    )
    missing_impl = _write_plan(
        docs_dir,
        "missing-impl",
        {"slug": "missing-impl", "title": "Missing Impl", "status": "shipped"},
    )
    stale_rca = _write_plan(
        docs_dir,
        "stale-rca",
        {"slug": "stale-rca", "title": "Stale RCA", "type": "research", "status": "active"},
    )
    clean = _write_plan(
        docs_dir,
        "clean-plan",
        {"slug": "clean-plan", "title": "Clean Plan", "status": "done", "impl": 1.0},
    )

    _age_file(stale, days=31)
    _age_file(missing_impl, days=5)
    _age_file(stale_rca, days=61)
    _age_file(clean, days=3)

    result = CliRunner().invoke(main, ["audit"])

    assert result.exit_code == 1
    assert "project" in result.output
    assert "stale-plan" in result.output
    assert "STALE" in result.output
    assert "missing-impl" in result.output
    assert "MISSING_IMPL" in result.output
    assert "stale-rca" in result.output
    assert "STALE_RCA" in result.output
    assert "clean-plan" not in result.output


def test_audit_project_filter_limits_output(tmp_path, monkeypatch):
    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(
        json.dumps({"proj-a": str(docs_a), "proj-b": str(docs_b)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))

    stale_a = _write_plan(
        docs_a,
        "stale-a",
        {"slug": "stale-a", "title": "Stale A", "status": "active", "impl": 0.2},
    )
    stale_b = _write_plan(
        docs_b,
        "stale-b",
        {"slug": "stale-b", "title": "Stale B", "status": "active", "impl": 0.3},
    )
    _age_file(stale_a, days=31)
    _age_file(stale_b, days=31)

    result = CliRunner().invoke(main, ["audit", "--project", "proj-b"])

    assert result.exit_code == 0
    assert "stale-b" in result.output
    assert "proj-b" in result.output
    assert "stale-a" not in result.output
    assert "proj-a" not in result.output
