"""Project-aware cross-type slug-collision audit for reckon.doccheck.

A plan and a resource of another type in one project can carry the same slug.
Every slug-only lookup on the plan then fails as "ambiguous across types",
while a per-file audit of either document passes. These tests build a synthetic
project under ``tmp_path`` — never a real mount — and assert the audit reports
the collision for either side, and reports nothing when the slugs are unique.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon import doccheck
from reckon.doccheck import run, slug_collision_findings


def _write_doc(docs_dir: Path, slug: str, artifact_type: str, relative: str) -> Path:
    content = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="reckon-type" content="{artifact_type}">'
        '<meta name="plan-status" content="active">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_shared_slug_reported_for_each_document(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    plan = _write_doc(docs_dir, "shared", "plan", "plans/shared.html")
    research = _write_doc(docs_dir, "shared", "research", "research/shared.html")

    for audited in (plan, research):
        findings = slug_collision_findings(audited, docs_dir=docs_dir, project="proj")
        assert [finding.severity for finding in findings] == ["error"]
        assert [finding.code for finding in findings] == ["slug-collision"]
        message = findings[0].message
        assert "plans/shared.html" in message
        assert "research/shared.html" in message


def test_shared_slug_run_exits_nonzero(tmp_path, monkeypatch, capsys):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_doc(docs_dir, "shared", "plan", "plans/shared.html")
    research = _write_doc(docs_dir, "shared", "research", "research/shared.html")

    monkeypatch.setattr(doccheck, "_load_mounts", lambda: {"proj": docs_dir.resolve()})

    exit_code = run([str(research)], project="proj")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "slug-collision" in out
    assert "plans/shared.html" in out


def test_shared_slug_cli_exits_nonzero(tmp_path, monkeypatch, capsys):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_doc(docs_dir, "shared", "plan", "plans/shared.html")
    research = _write_doc(docs_dir, "shared", "research", "research/shared.html")

    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"proj": str(docs_dir.resolve())}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))

    exit_code = doccheck.main(["--project", "proj", str(research)])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "slug-collision" in out
    assert "research/shared.html" in out


def test_unique_slugs_produce_no_collision(tmp_path, monkeypatch, capsys):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    plan = _write_doc(docs_dir, "alpha", "plan", "plans/alpha.html")
    research = _write_doc(docs_dir, "beta", "research", "research/beta.html")

    assert slug_collision_findings(plan, docs_dir=docs_dir, project="proj") == []
    assert slug_collision_findings(research, docs_dir=docs_dir, project="proj") == []

    monkeypatch.setattr(doccheck, "_load_mounts", lambda: {"proj": docs_dir.resolve()})

    exit_code = run([str(plan), str(research)], project="proj")

    out = capsys.readouterr().out
    assert "slug-collision" not in out
    assert exit_code == 0
