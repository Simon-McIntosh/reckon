"""The shared DAG layout: depth, routing, arrival fan and stage extent.

Every assertion runs the real exported entry point — `window.ReckonGraph.layout`
in the compiled module — rather than a copy of its body, so a helper that works
but is never exported fails here.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from tests.spa_module_eval import evaluate_jsx_module

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs" / "ui" / "graph.jsx"

# Six plans over four depths. `surface` hangs off prerequisites at depths 0 and
# 2, which makes it both the fan-in case (two arrivals on one card) and the
# skip-level case (an edge from depth 0 crossing depths 1 and 2). `stray`
# depends on a slug outside the drawn set and `context` is a ghost.
FIXTURE = [
    {
        "slug": "substrate",
        "title": "Substrate",
        "status": "shipped",
        "impl": 1.0,
        "effort_hours": 12,
        "depends_on": [],
    },
    {
        "slug": "parser",
        "title": "Parser",
        "status": "shipped",
        "impl": 1.0,
        "effort_hours": 8,
        "depends_on": ["substrate"],
    },
    {
        "slug": "router",
        "title": "Router",
        "status": "active",
        "impl": 0.4,
        "effort_hours": 16,
        "depends_on": ["parser"],
    },
    {
        "slug": "surface",
        "title": "Surface",
        "status": "blocked",
        "impl": 0.0,
        "effort_hours": 20,
        "depends_on": ["substrate", "router"],
    },
    {
        "slug": "context",
        "title": "Context",
        "status": "pending",
        "impl": 0.0,
        "effort_hours": 6,
        "depends_on": [],
        "ghost": True,
        "sprint": "S12",
    },
    {
        "slug": "stray",
        "title": "Stray",
        "status": "pending",
        "impl": 0.0,
        "effort_hours": 3,
        "depends_on": ["a-plan-nobody-drew"],
    },
]

CYCLE_FIXTURE = [
    {
        "slug": "loop-one",
        "title": "Loop One",
        "status": "pending",
        "depends_on": ["loop-two"],
    },
    {
        "slug": "loop-two",
        "title": "Loop Two",
        "status": "pending",
        "depends_on": ["loop-one"],
    },
]


def _layout(plans: list[dict], prefix: str = "t") -> dict:
    expression = f"window.ReckonGraph.layout({json.dumps(plans)}, {json.dumps(prefix)})"
    return evaluate_jsx_module(GRAPH, expression)


def _nodes(layout: dict) -> dict[str, dict]:
    return {node["slug"]: node for node in layout["nodes"]}


def _edge(layout: dict, source: str, target: str) -> dict:
    matches = [
        edge
        for edge in layout["edges"]
        if edge["from"] == source and edge["to"] == target
    ]
    assert len(matches) == 1, f"expected one {source}->{target} edge, got {matches}"
    return matches[0]


def test_depth_is_one_more_than_the_deepest_known_prerequisite() -> None:
    layout = _layout(FIXTURE)
    assert layout["depth"] == {
        "substrate": 0,
        "parser": 1,
        "router": 2,
        # Prerequisites at depths 0 and 2 put this plan at 3, not at 1.
        "surface": 3,
        "context": 0,
        "stray": 0,
    }


def test_a_dependency_outside_the_drawn_set_contributes_no_depth_and_no_edge() -> None:
    layout = _layout(FIXTURE)
    assert layout["depth"]["stray"] == 0
    assert not [
        edge
        for edge in layout["edges"]
        if edge["to"] == "stray" or edge["from"] == "a-plan-nobody-drew"
    ]


def test_a_two_node_cycle_terminates_with_finite_depths() -> None:
    layout = _layout(CYCLE_FIXTURE)
    depths = layout["depth"]
    assert set(depths) == {"loop-one", "loop-two"}
    for slug, value in depths.items():
        assert isinstance(value, int | float), f"{slug} depth is {value!r}"
        assert math.isfinite(value), f"{slug} depth {value}"
    assert len(layout["edges"]) == 2


def test_cards_are_216_by_82_on_92_and_22_gaps() -> None:
    layout = _layout(FIXTURE)
    nodes = _nodes(layout)
    assert {(node["width"], node["height"]) for node in nodes.values()} == {(216, 82)}
    # Column pitch: card width plus the horizontal gap.
    assert nodes["parser"]["x"] - nodes["substrate"]["x"] == 216 + 92
    # Row pitch inside the first column, whose members sort by slug.
    first_column = sorted(
        (node for node in nodes.values() if node["depth"] == 0),
        key=lambda node: node["y"],
    )
    assert [node["slug"] for node in first_column] == ["context", "stray", "substrate"]
    assert first_column[1]["y"] - first_column[0]["y"] == 82 + 22


def test_an_edge_spanning_two_depths_runs_below_every_column_it_crosses() -> None:
    layout = _layout(FIXTURE)
    nodes = _nodes(layout)
    edge = _edge(layout, "substrate", "surface")
    assert edge["skip"] is True
    assert edge["span"] == 3
    crossed_bottom = max(
        node["y"] + node["height"] for node in nodes.values() if node["depth"] in (1, 2)
    )
    assert edge["detourY"] >= crossed_bottom + 24, (
        f"detour at {edge['detourY']} does not clear {crossed_bottom} + 24"
    )
    # The straight run is an explicit L command at exactly that depth, not a
    # cubic whose peak only approaches it.
    straight = re.search(r"L (-?[\d.]+) (-?[\d.]+)", edge["d"])
    assert straight, edge["d"]
    assert float(straight.group(2)) == edge["detourY"]
    assert edge["d"].count(" C ") == 2


def test_an_edge_spanning_one_depth_is_a_cubic_with_no_detour() -> None:
    layout = _layout(FIXTURE)
    edge = _edge(layout, "parser", "router")
    assert edge["skip"] is False
    assert edge["detourY"] is None
    assert " L " not in edge["d"]
    assert edge["d"].count(" C ") == 1


def test_co_terminating_edges_arrive_8px_apart_around_the_card_centre() -> None:
    layout = _layout(FIXTURE)
    arriving = [edge for edge in layout["edges"] if edge["to"] == "surface"]
    assert len(arriving) == 2
    low, high = sorted(edge["endY"] for edge in arriving)
    assert high - low == 8
    centre = _nodes(layout)["surface"]["y"] + 82 / 2
    assert (low + high) / 2 == centre


def test_the_stage_clears_the_deepest_detour_by_30() -> None:
    layout = _layout(FIXTURE)
    detours = [
        edge["detourY"] for edge in layout["edges"] if edge["detourY"] is not None
    ]
    assert detours
    assert layout["height"] >= max(detours) + 30
    # And it still clears the row extent when no detour is emitted.
    shallow = _layout(
        [plan for plan in FIXTURE if plan["slug"] != "surface"],
    )
    rows = max(node["y"] + node["height"] for node in shallow["nodes"])
    assert shallow["height"] >= rows
    assert not [edge for edge in shallow["edges"] if edge["skip"]]


def test_stroke_semantics_read_the_prerequisite_and_the_dependent() -> None:
    layout = _layout(FIXTURE)
    # A blocked plan hanging off an unshipped prerequisite: red and heavier.
    held = _edge(layout, "router", "surface")
    assert held["held"] is True
    assert held["stroke"] == "oklch(0.58 0.20 25)"
    assert held["strokeWidth"] > _edge(layout, "substrate", "parser")["strokeWidth"]
    # Shipped prerequisite: solid, and not held even into a blocked plan.
    shipped = _edge(layout, "substrate", "surface")
    assert shipped["dashed"] is False
    assert shipped["dash"] == "0"
    assert shipped["held"] is False
    # Unshipped prerequisite into an unblocked plan: dashed, ordinary stroke.
    pending = _edge(
        _layout(
            [
                {"slug": "upstream", "status": "active", "depends_on": []},
                {"slug": "downstream", "status": "pending", "depends_on": ["upstream"]},
            ]
        ),
        "upstream",
        "downstream",
    )
    assert pending["dashed"] is True
    assert pending["dash"] == "4 3"
    assert pending["held"] is False
    assert pending["stroke"] == "#c9ccd4"


def test_a_ghost_node_is_dashed_dimmed_and_names_its_sprint() -> None:
    nodes = _nodes(_layout(FIXTURE))
    ghost = nodes["context"]
    assert ghost["ghost"] is True
    assert ghost["borderStyle"] == "dashed"
    assert ghost["background"] == "transparent"
    assert ghost["opacity"] < 1
    assert "S12" in ghost["statusText"]
    solid = nodes["router"]
    assert solid["borderStyle"] == "solid"
    assert solid["opacity"] == 1
    assert solid["statusText"] == "active"


def test_a_ghost_without_a_sprint_says_so_rather_than_reading_bare() -> None:
    fixture = [{**FIXTURE[4], "sprint": None}]
    ghost = _nodes(_layout(fixture))["context"]
    assert ghost["statusText"] == "pending · unscheduled"


def test_columns_are_labelled_and_the_stage_spans_them() -> None:
    layout = _layout(FIXTURE)
    assert [column["label"] for column in layout["columns"]] == [
        "no prerequisites",
        "depth 1",
        "depth 2",
        "depth 3",
    ]
    assert [column["depth"] for column in layout["columns"]] == [0, 1, 2, 3]
    assert [column["x"] for column in layout["columns"]] == [0, 308, 616, 924]
    assert layout["width"] == 4 * (216 + 92) - 92 + 20


def test_node_and_edge_keys_carry_the_callers_prefix() -> None:
    layout = _layout(FIXTURE, prefix="sd")
    assert all(node["key"].startswith("sd-") for node in layout["nodes"])
    assert _edge(layout, "parser", "router")["key"] == "sd-parser-router"


def test_cards_carry_the_hours_and_completion_a_surface_renders() -> None:
    nodes = _nodes(_layout(FIXTURE))
    assert nodes["router"]["hours"] == "16h"
    assert nodes["router"]["percent"] == 40
    assert nodes["substrate"]["percent"] == 100
    # An isolate reads as one; a card in the chain does not.
    assert nodes["stray"]["connected"] is False
    assert nodes["router"]["connected"] is True


def test_the_layout_is_exported_for_both_consuming_surfaces() -> None:
    source = GRAPH.read_text(encoding="utf-8")
    assert re.search(r"window\.ReckonGraph\s*=\s*\{[^}]*layout:", source)
    published = evaluate_jsx_module(GRAPH, "Object.keys(window.ReckonGraph).sort()")
    assert published == ["geometry", "layout"]
    geometry = evaluate_jsx_module(GRAPH, "window.ReckonGraph.geometry")
    assert geometry["cardWidth"] == 216
    assert geometry["cardHeight"] == 82
    assert geometry["columnGap"] == 92
    assert geometry["rowGap"] == 22
    assert geometry["detourClearance"] == 24
    assert geometry["arrivalFan"] == 8


def test_an_empty_set_lays_out_without_throwing() -> None:
    layout = _layout([])
    assert layout["nodes"] == []
    assert layout["edges"] == []
    assert layout["columns"] == []
    assert layout["width"] > 0
    assert layout["height"] > 0
