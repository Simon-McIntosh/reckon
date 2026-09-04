from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from reckon.roadmap import GraphTargetError, build_roadmap, resolve_graph_target
from reckon.serve import discover_plans

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs" / "ui" / "graph.jsx"


def _plan(
    slug: str,
    *,
    depends_on: list[str] | None = None,
    handle: str | None = None,
    status: str = "active",
    decisions: list[dict] | None = None,
) -> dict:
    return {
        "project": "sample",
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "type": "plan",
        "status": status,
        "impl": 1.0 if status == "shipped" else 0.0,
        "depends_on": depends_on or [],
        "graph_handle": handle,
        "decisions": decisions or [],
        "blocking": [],
        "gates": [],
        "followups": [],
    }


def _endpoint_fixture() -> list[dict]:
    return [
        _plan("foundation", status="shipped"),
        _plan(
            "middle",
            depends_on=["foundation"],
            status="blocked",
            decisions=[{"key": "scope", "choice": ""}],
        ),
        _plan("named-deep", depends_on=["middle"], handle="deep"),
        _plan("named-shallow", depends_on=["foundation"], handle="shallow"),
        _plan("unnamed-deep", depends_on=["middle"]),
        _plan("unnamed-left", depends_on=["foundation"]),
        _plan("unnamed-right", depends_on=["foundation"]),
        _plan("standalone"),
    ]


def test_project_roadmap_enumerates_named_and_unnamed_endpoints() -> None:
    rows = build_roadmap("sample", _endpoint_fixture(), [], review={})["endpoints"]

    assert len(rows) == 5
    assert [row["slug"] for row in rows] == [
        "named-deep",
        "named-shallow",
        "unnamed-deep",
        "unnamed-left",
        "unnamed-right",
    ]
    assert [row["handle"] for row in rows] == ["deep", "shallow", None, None, None]
    assert all("handle" in row for row in rows)
    assert rows[0]["completion"] == {"shipped": 1, "total": 3}
    assert rows[0]["shipped_fraction"] == 0.333
    assert rows[0]["held"] == 1
    assert rows[0]["open_decision_count"] == 1
    assert rows[0]["structural_depth"] == 3
    assert rows[0]["average_width"] == 1.0
    assert rows[0]["repositories"] == [{"repository": "sample", "count": 3}]


def test_unnamed_endpoint_matches_the_authored_handle_read() -> None:
    inventory = [
        _plan("source", status="shipped"),
        _plan("middle", depends_on=["source"]),
        _plan("endpoint", depends_on=["middle"]),
    ]
    unnamed = build_roadmap("sample", inventory, [], review={})["endpoints"][0]

    authored = deepcopy(inventory)
    authored[-1]["graph_handle"] = "release"
    named = resolve_graph_target("release", {"sample": authored})

    assert {member["ref"] for member in unnamed["members"]} == {
        member["ref"] for member in named["members"]
    }
    assert unnamed["structural_depth"] == named["critical_path"]["depth"]
    assert unnamed["completion"] == named["completion"]
    assert [row["repository"] for row in unnamed["repositories"]] == named[
        "repositories"
    ]


def test_project_without_prerequisites_has_no_endpoints() -> None:
    inventory = [_plan("left"), _plan("right")]

    assert build_roadmap("sample", inventory, [], review={})["endpoints"] == []


def test_unknown_graph_handle_still_refuses() -> None:
    with pytest.raises(GraphTargetError, match="names no live plan"):
        resolve_graph_target("unknown", {"sample": _endpoint_fixture()})


def test_graph_endpoint_rows_equal_the_project_roadmap_rows() -> None:
    discovered = discover_plans(ROOT / "docs", "reckon", None)
    report = build_roadmap(
        "reckon",
        discovered["inventory"],
        discovered["sprints"],
        active_sprint_id=discovered.get("active_sprint_id"),
        project_manifest=discovered,
        review={},
    )
    source = GRAPH.read_text(encoding="utf-8")
    helpers = source.split("function PathPromptModal", 1)[0]
    script = f"""
const endpoints = {json.dumps(report["endpoints"])};
{helpers}
const rows = _roadmapEndpointRows(endpoints);
console.log(JSON.stringify(rows.map(row => ({{
  slug: row.slug,
  members: row.members.map(member => member.ref).sort(),
}}))));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    rendered = json.loads(result.stdout)
    expected = [
        {
            "slug": row["slug"],
            "members": sorted(member["ref"] for member in row["members"]),
        }
        for row in report["endpoints"]
    ]

    assert rendered == expected
    assert "_roadmapEndpointRows(M?.endpoints)" in source
