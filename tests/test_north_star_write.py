from __future__ import annotations

import json
from pathlib import Path

import reckon._store as store_module
import reckon.mcp as mcp_module
from reckon._plan_html import write_state

_PLAN_SET_FIELDS_BEFORE_NORTH_STAR = frozenset(
    {
        "archived",
        "artifacts",
        "blocks",
        "capability",
        "commits",
        "depends_on",
        "effort",
        "effort_hours",
        "environment",
        "evidence_for",
        "graph_handle",
        "impl",
        "informs",
        "milestone",
        "owner",
        "read",
        "recorded_at",
        "reviewed_at",
        "roi",
        "slug",
        "source",
        "source_quality",
        "sprint",
        "status",
        "summary",
        "supersedes",
        "title",
        "type",
        "verdict",
        "verifies",
    }
)


def _write_fixture(checkout: Path) -> Path:
    project = "sample"
    docs = checkout / "docs"
    plan_path = docs / "plans" / "delivery.html"
    plan_path.parent.mkdir(parents=True)
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{project}">'
        "<title>Delivery</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    plan_path.write_text(
        write_state(
            bare,
            {
                "slug": "delivery",
                "title": "Delivery",
                "status": "active",
                "type": "plan",
                "version": 0,
            },
        ),
        encoding="utf-8",
    )

    direction = {
        "id": "reliable-delivery",
        "name": "Reliable delivery",
        "statement": "Every accepted change is reproducible and observable.",
    }
    state_dir = docs / "state" / project
    state_dir.mkdir(parents=True)
    (state_dir / "index.json").write_text(
        json.dumps(
            {
                "project": project,
                "data": {
                    "north_stars": [direction],
                    "projects": [{"project": project, "north_stars": [direction]}],
                },
            }
        ),
        encoding="utf-8",
    )
    return plan_path


def _unoriented_count(report: dict) -> int:
    return sum(
        finding.get("code") == "unoriented-plan"
        for finding in report["wiring_findings"]
    )


def test_state_edit_sets_validated_north_star_and_clears_finding(tmp_path) -> None:
    plan_path = _write_fixture(tmp_path)
    before = mcp_module._roadmap("sample", checkout_path=str(tmp_path))

    result = mcp_module._edit_plan(
        "sample",
        "delivery",
        [{"op": "set", "path": "north_star", "value": "reliable-delivery"}],
        expected_version=0,
        checkout_path=str(tmp_path),
        mode="state",
    )

    assert result["ok"] is True
    parsed, version = store_module.read_plan("sample", "delivery", root=tmp_path)
    assert version == result["new_version"] == 1
    assert parsed["north_star"] == "reliable-delivery"
    assert 'name="plan-north-star" content="reliable-delivery"' in (
        plan_path.read_text(encoding="utf-8")
    )

    after = mcp_module._roadmap("sample", checkout_path=str(tmp_path))
    assert (_unoriented_count(before), _unoriented_count(after)) == (1, 0)

    written = plan_path.read_bytes()
    refused = mcp_module._edit_plan(
        "sample",
        "delivery",
        [{"op": "set", "path": "north_star", "value": {"invalid": "shape"}}],
        expected_version=version,
        checkout_path=str(tmp_path),
        mode="state",
    )
    assert refused["ok"] is False
    assert refused["error"] == "schema_validation"
    assert plan_path.read_bytes() == written


def test_north_star_is_the_only_plan_set_field_added() -> None:
    assert {
        "north_star"
    } == store_module._PLAN_SET_TOP - _PLAN_SET_FIELDS_BEFORE_NORTH_STAR
    assert set() == _PLAN_SET_FIELDS_BEFORE_NORTH_STAR - store_module._PLAN_SET_TOP
