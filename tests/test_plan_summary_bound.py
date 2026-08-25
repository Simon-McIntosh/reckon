from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
from reckon._plan_html import write_state
from reckon.doccheck import audit_html, audit_lifecycle


@pytest.fixture()
def mounted_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = "sample"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    importlib.reload(store_module)
    importlib.reload(mcp_module)
    return project, docs_dir


def _write_plan(docs_dir: Path, slug: str, summary: str) -> Path:
    source = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    state = {
        "slug": slug,
        "title": slug,
        "status": "active",
        "summary": summary,
        "version": 0,
    }
    path = docs_dir / f"{slug}.html"
    path.write_text(write_state(source, state), encoding="utf-8")
    return path


def test_summary_write_refuses_measured_overflow_but_allows_unrelated_edit(
    mounted_project,
):
    project, docs_dir = mounted_project
    existing = "x" * (store_module.PLAN_SUMMARY_MAX_LENGTH + 7)
    _write_plan(docs_dir, "existing", existing)

    unrelated = mcp_module._edit_plan(
        project,
        "existing",
        [{"op": "set", "path": "roi", "value": "high"}],
        0,
    )
    assert unrelated["ok"] is True
    _, current_version = store_module.read_plan(project, "existing")

    measured = store_module.PLAN_SUMMARY_MAX_LENGTH + 1
    refused = mcp_module._edit_plan(
        project,
        "existing",
        [{"op": "set", "path": "summary", "value": "y" * measured}],
        current_version,
    )
    assert refused["ok"] is False
    assert refused["error"] == "op_error"
    assert str(store_module.PLAN_SUMMARY_MAX_LENGTH) in refused["detail"]
    assert str(measured) in refused["detail"]


def test_audits_report_every_existing_overlong_summary_with_its_length(
    mounted_project,
):
    project, docs_dir = mounted_project
    lengths = {"first": 161, "second": 219}
    paths = {
        slug: _write_plan(docs_dir, slug, "x" * length)
        for slug, length in lengths.items()
    }

    lifecycle = audit_lifecycle(project=project, docs_dir=docs_dir)
    reported = {
        finding.slug: finding.summary_length
        for finding in lifecycle
        if finding.flag == "SUMMARY_TOO_LONG"
    }
    assert reported == lengths
    for slug, length in lengths.items():
        finding = next(
            item
            for item in audit_html(paths[slug].read_text(encoding="utf-8"))
            if item.code == "summary-too-long"
        )
        assert str(length) in finding.message
        assert str(store_module.PLAN_SUMMARY_MAX_LENGTH) in finding.message


def test_exact_bound_with_apostrophes_is_clean_at_write_and_both_audits(
    mounted_project,
):
    project, docs_dir = mounted_project
    path = _write_plan(docs_dir, "apostrophes", "short")
    summary = "'" + "x" * (store_module.PLAN_SUMMARY_MAX_LENGTH - 2) + "'"

    written = mcp_module._edit_plan(
        project,
        "apostrophes",
        [{"op": "set", "path": "summary", "value": summary}],
        0,
    )
    html = path.read_text(encoding="utf-8")
    encoded_summary = html.split('name="plan-summary" content="', 1)[1].split(
        '"', 1
    )[0]
    document_findings = audit_html(html)
    lifecycle_findings = audit_lifecycle(project=project, docs_dir=docs_dir)

    assert len(summary) == store_module.PLAN_SUMMARY_MAX_LENGTH
    assert len(encoded_summary) == store_module.PLAN_SUMMARY_MAX_LENGTH + 10
    assert written["ok"] is True
    assert html.count("&#x27;") == 2
    assert not any(item.code == "summary-too-long" for item in document_findings)
    assert not any(
        item.flag == "SUMMARY_TOO_LONG" for item in lifecycle_findings
    )


def test_authoring_skills_state_the_summary_bound():
    root = Path(__file__).parents[1]
    for relative in (
        "skills/reckon-create/SKILL.md",
        "skills/reckon-edit/SKILL.md",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert "plan-summary" in text
        assert "160 characters" in text
