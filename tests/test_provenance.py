"""Vertical contract tests for typed research and evidence provenance."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
from reckon._plan_html import read_state, write_state
from reckon._schema import PlanState
from reckon.doccheck import audit_links
from reckon.serve import discover_plans


def _bare(project: str = "proj") -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        "<title>Artifact | proj</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )


def _write_artifact(docs: Path, slug: str, state: dict) -> Path:
    path = docs / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(
            _bare(),
            {
                "slug": slug,
                "title": state.get("title", slug.title()),
                **state,
            },
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def mounted(tmp_path, monkeypatch):
    project = "proj"
    docs = tmp_path / "docs"
    docs.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    importlib.reload(store_module)
    importlib.reload(mcp_module)
    return project, docs


def test_legacy_doc_reads_as_canonical_research():
    html = (
        '<html><head><meta name="reckon-type" content="doc">'
        '<meta name="docs-project" content="proj">'
        '<meta name="plan-slug" content="legacy">'
        "<title>Legacy</title></head><body></body></html>"
    )
    assert read_state(html)["type"] == "research"
    assert PlanState.model_validate(read_state(html)).type == "research"


def test_evidence_round_trip_preserves_qualified_provenance():
    state = {
        "type": "evidence",
        "slug": "verification",
        "title": "Verification",
        "recorded_at": "2026-07-29T13:30:00+02:00",
        "verdict": "pass",
        "environment": "linux-x86_64",
        "evidence_for": ["proj:alpha", "legacy-plan"],
        "verifies": ["proj:alpha#parser", "legacy-plan#ui"],
        "supersedes": ["proj:old-evidence"],
        "commits": ["abc1234", "def5678"],
        "artifacts": ["reports/results.json", "coverage/index.html"],
    }
    rendered = write_state(_bare(), state)
    parsed = read_state(rendered)
    assert parsed["type"] == "evidence"
    for field, value in state.items():
        assert parsed[field] == value
    assert 'name="plan-evidence-for"' in rendered
    assert 'name="plan-recorded-at"' in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("impl", 0.5),
        ("status", "active"),
        ("sprint", "S1"),
        ("roi", "high"),
        ("effort", "L"),
        ("milestone", "M1"),
        ("tier", "opus"),
        ("depends_on", ["alpha"]),
        ("blocks", ["beta"]),
    ],
)
def test_non_plan_strict_write_rejects_meaningful_plan_fields(field, value):
    state = PlanState.model_validate(
        {
            "project": "proj",
            "type": "evidence",
            "slug": "verification",
            "title": "Verification",
            field: value,
        }
    )
    with pytest.raises(ValueError, match=field):
        state.validate_for_write()


def test_non_plan_canonical_dump_omits_neutral_plan_defaults():
    state = PlanState.model_validate(
        {
            "project": "proj",
            "type": "research",
            "slug": "study",
            "title": "Study",
            "status": "reference",
            "impl": 0,
            "roi": "mid",
            "effort": "M",
            "milestone": "—",
            "tier": "sonnet",
        }
    ).validate_for_write()
    dumped = state.canonical_dump()
    assert not {
        "status",
        "impl",
        "roi",
        "effort",
        "milestone",
        "tier",
    }.intersection(dumped)


def test_edit_plan_emits_canonical_type_and_strips_plan_defaults(mounted):
    project, docs = mounted
    path = docs / "legacy.html"
    path.write_text(
        '<html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="doc">'
        '<meta name="plan-slug" content="legacy">'
        '<meta name="plan-title" content="Legacy">'
        '<meta name="plan-status" content="reference">'
        '<meta name="plan-impl" content="0">'
        "<title>Legacy</title></head><body><main></main></body></html>",
        encoding="utf-8",
    )
    result = mcp_module._edit_plan(
        project,
        "legacy",
        [{"op": "set", "path": "reviewed_at", "value": "2026-07-29"}],
        expected_version=0,
    )
    assert result["ok"] is True
    text = path.read_text(encoding="utf-8")
    assert 'name="reckon-type" content="research"' in text
    assert 'name="plan-reviewed-at" content="2026-07-29"' in text
    assert 'name="plan-status"' not in text
    assert 'name="plan-impl"' not in text


def test_discovery_and_mcp_filters_keep_artifact_facets_distinct(mounted):
    project, docs = mounted
    _write_artifact(
        docs,
        "alpha",
        {"type": "plan", "status": "active", "impl": 0.25, "sprint": "S1"},
    )
    _write_artifact(
        docs,
        "study",
        {
            "type": "research",
            "informs": ["proj:alpha"],
            "reviewed_at": "2026-07-28",
            "source": "field audit",
            "source_quality": "reviewed",
        },
    )
    _write_artifact(
        docs,
        "verification",
        {
            "type": "evidence",
            "evidence_for": ["proj:alpha"],
            "verdict": "pass",
            "recorded_at": "2026-07-29",
        },
    )

    inventory = discover_plans(docs, project, None)["inventory"]
    by_slug = {item["slug"]: item for item in inventory}
    assert {item["type"] for item in inventory} == {"plan", "research", "evidence"}
    assert by_slug["alpha"]["impl"] == 0.25
    assert "impl" not in by_slug["study"]
    assert by_slug["study"]["source_quality"] == "reviewed"
    assert "sprint" not in by_slug["verification"]

    evidence = mcp_module._read_plan(project, doc_type="evidence")
    research = mcp_module._read_plan(project, doc_type="doc")
    assert [item["slug"] for item in evidence["plans"]] == ["verification"]
    assert [item["slug"] for item in research["plans"]] == ["study"]
    assert evidence["summary"]["by_type"] == {"evidence": 1}
    assert evidence["summary"]["plans"] == 0
    assert evidence["summary"]["artifacts"] == 1
    assert evidence["summary"]["impl_mean"] == 0.0


def test_audit_warns_for_unlinked_artifacts_and_excludes_archive(mounted):
    project, docs = mounted
    _write_artifact(docs, "study", {"type": "research"})
    _write_artifact(docs, "verification", {"type": "evidence"})
    _write_artifact(
        docs / "archive",
        "old-stage",
        {"type": "plan", "status": "not-a-status"},
    )

    result = mcp_module._audit(project)
    codes = {(item["severity"], item["code"]) for item in result["findings"]}
    assert ("warn", "unlinked-research") in codes
    assert ("warn", "unlinked-evidence") in codes
    assert result["checked"] == 2
    assert all(item["slug"] != "old-stage" for item in result["violations"])


def test_unactivated_definition_pages_are_not_artifacts_or_rollups(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sprints").mkdir(parents=True)
    (docs / "milestones").mkdir()
    _write_artifact(docs, "alpha", {"type": "plan", "status": "active"})
    (docs / "sprints" / "S1.html").write_text(
        '<html><head><meta name="sprint-id" content="S1">'
        '<meta name="sprint-theme" content="Typed artifacts">'
        '<meta name="sprint-status" content="active"></head></html>',
        encoding="utf-8",
    )
    (docs / "milestones" / "M1.html").write_text(
        '<html><head><meta name="milestone-id" content="M1">'
        '<meta name="milestone-name" content="Provenance">'
        '<meta name="milestone-status" content="active"></head></html>',
        encoding="utf-8",
    )

    discovered = discover_plans(docs, "proj", None)
    assert [item["slug"] for item in discovered["inventory"]] == ["alpha"]
    assert discovered["sprints"] == []
    assert discovered["milestones"] == []


def test_link_audit_understands_qualified_and_stage_refs(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    target = _write_artifact(docs, "alpha", {"type": "plan", "status": "active"})
    source = _write_artifact(
        docs,
        "verification",
        {
            "type": "evidence",
            "evidence_for": ["proj:alpha", "other:remote"],
            "verifies": ["proj:alpha#parser"],
        },
    )
    assert audit_links([source, target], docs, project="proj") == {}


def test_spa_sources_keep_typed_facets_and_provenance_direction():
    root = Path(__file__).resolve().parents[1]
    shell = (root / "docs/ui/shell.jsx").read_text(encoding="utf-8")
    plan = (root / "docs/ui/plan.jsx").read_text(encoding="utf-8")
    graph = (root / "docs/ui/graph.jsx").read_text(encoding="utf-8")
    home = (root / "docs/ui/home.jsx").read_text(encoding="utf-8")
    assert all(label in shell for label in ("Plans", "Research", "Evidence", "Archive"))
    assert "evidence_for" in plan and "verifies" in plan
    assert "research → plan → evidence" in graph
    assert "p.type" in home and ".slice(0, 40)" in home
