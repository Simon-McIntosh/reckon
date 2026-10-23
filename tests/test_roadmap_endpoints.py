from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from reckon.roadmap import GraphTargetError, build_roadmap, resolve_graph_target
from reckon.serve import discover_plans
from tests.roadmap_fixtures import plan_row

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs" / "ui" / "graph.jsx"


def _endpoint_fixture() -> list[dict]:
    return [
        plan_row(
            "foundation",
            title="Foundation",
            status="shipped",
            impl=1.0,
            gates=[],
            followups=[],
        ),
        plan_row(
            "middle",
            title="Middle",
            depends_on=["foundation"],
            status="blocked",
            decisions=[{"key": "scope", "choice": ""}],
            gates=[],
            followups=[],
        ),
        plan_row(
            "named-deep",
            title="Named Deep",
            depends_on=["middle"],
            graph_handle="deep",
            gates=[],
            followups=[],
        ),
        plan_row(
            "named-shallow",
            title="Named Shallow",
            depends_on=["foundation"],
            graph_handle="shallow",
            gates=[],
            followups=[],
        ),
        plan_row(
            "unnamed-deep",
            title="Unnamed Deep",
            depends_on=["middle"],
            gates=[],
            followups=[],
        ),
        plan_row(
            "unnamed-left",
            title="Unnamed Left",
            depends_on=["foundation"],
            gates=[],
            followups=[],
        ),
        plan_row(
            "unnamed-right",
            title="Unnamed Right",
            depends_on=["foundation"],
            gates=[],
            followups=[],
        ),
        plan_row("standalone", title="Standalone", gates=[], followups=[]),
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
    # The endpoint carries the closure's progress roll-up: one shipped member of
    # three, no authored hours, so the plain mean of the members' figures.
    assert rows[0]["progress"] == pytest.approx(1 / 3)
    assert rows[0]["held"] == 1
    assert rows[0]["open_decision_count"] == 1
    assert rows[0]["structural_depth"] == 3
    assert rows[0]["average_width"] == 1.0
    assert rows[0]["repositories"] == [{"repository": "sample", "count": 3}]


def test_unnamed_endpoint_matches_the_authored_handle_read() -> None:
    inventory = [
        plan_row(
            "source", title="Source", status="shipped", impl=1.0, gates=[], followups=[]
        ),
        plan_row(
            "middle", title="Middle", depends_on=["source"], gates=[], followups=[]
        ),
        plan_row(
            "endpoint", title="Endpoint", depends_on=["middle"], gates=[], followups=[]
        ),
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
    assert unnamed["progress"] == pytest.approx(named["progress"])
    assert [row["repository"] for row in unnamed["repositories"]] == named[
        "repositories"
    ]


def test_project_without_prerequisites_has_no_endpoints() -> None:
    inventory = [
        plan_row("left", title="Left", gates=[], followups=[]),
        plan_row("right", title="Right", gates=[], followups=[]),
    ]

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
