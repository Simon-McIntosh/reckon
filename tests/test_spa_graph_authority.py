import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs" / "ui" / "graph.jsx"


def _evaluate_graph_helpers(inventory: list[dict], endpoint: str) -> dict:
    source = GRAPH.read_text(encoding="utf-8")
    helper_source = source.split("function PathPromptModal", 1)[0]
    script = f"""
const inventory = {json.dumps(inventory)};
{helper_source}
const measure = _dependencyChainMeasure(inventory);
const closure = _dependencyClosure({json.dumps(endpoint)}, measure.bySlug);
const members = [...closure].map(slug => measure.bySlug[slug]);
const view = _graphHandleView(
  measure.bySlug[{json.dumps(endpoint)}],
  members,
  measure.pathLen[{json.dumps(endpoint)}],
  "alpha",
);
const chains = _allDependencyChains(inventory);
console.log(JSON.stringify({{ view, chains }}));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_graph_handle_derives_membership_authority_and_ship_hold() -> None:
    result = _evaluate_graph_helpers(
        [
            {
                "slug": "source",
                "project": "beta",
                "status": "shipped",
                "depends_on": [],
                "decisions": [],
            },
            {
                "slug": "middle",
                "project": "alpha",
                "status": "active",
                "depends_on": ["beta:source"],
                "decisions": [
                    {"key": "scope", "choice": "", "chosen": ""},
                ],
            },
            {
                "slug": "endpoint",
                "project": "alpha",
                "title": "Release endpoint",
                "status": "pending",
                "graph_handle": "release",
                "depends_on": ["middle"],
                "decisions": [],
            },
        ],
        "endpoint",
    )
    view = result["view"]

    assert view["handle"] == "release"
    assert view["shipLine"] == "/reckon-ship release"
    assert {member["slug"] for member in view["members"]} == {
        "source",
        "middle",
        "endpoint",
    }
    assert view["repositories"] == [
        {"repository": "alpha", "count": 2},
        {"repository": "beta", "count": 1},
    ]
    assert view["openDecisions"] == 1
    assert view["structuralDepth"] == 3
    assert view["averageWidth"] == "1.00"
    assert result["chains"][0][-1] == "endpoint"

    source = GRAPH.read_text(encoding="utf-8")
    assert "disabled={graphHandle.openDecisions > 0}" in source
    assert "Derived closure membership" in source
    assert "repositories enter scope only through closure membership" in source


def test_hop_count_is_labelled_as_structural_not_execution_ordering() -> None:
    source = GRAPH.read_text(encoding="utf-8")

    assert "longest dependency chain by hop count" in source
    assert "structural depth only; not execution ordering" in source
    assert "earliest finish" not in source
