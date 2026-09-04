from __future__ import annotations

import re
from pathlib import Path

from tests.spa_module_eval import evaluate_jsx_module

ROOT = Path(__file__).resolve().parents[1]
STYLESHEET = ROOT / "docs" / "ui" / "graph.css"
COMPONENT = ROOT / "docs" / "ui" / "graph.jsx"


def _rule(source: str, selector: str) -> dict[str, str]:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    for match in re.finditer(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]+)\}", source):
        selectors = {item.strip() for item in match.group("selectors").split(",")}
        if selector not in selectors:
            continue
        return {
            name.strip(): value.strip()
            for declaration in match.group("body").split(";")
            if ":" in declaration
            for name, value in [declaration.split(":", 1)]
        }
    raise AssertionError(f"missing rule for {selector}")


def test_graph_surface_header_and_handle_match_canvas_geometry() -> None:
    source = STYLESHEET.read_text(encoding="utf-8")

    assert _rule(source, ".r-graph") == {
        "flex": "1",
        "overflow": "auto",
        "padding": "20px 26px 40px",
    }
    header = _rule(source, ".r-graph-header")
    assert header["display"] == "flex"
    assert header["align-items"] == "center"
    assert header["gap"] == "10px"
    assert header["padding-bottom"] == "13px"
    assert header["border-bottom"] == "1px solid var(--line)"

    handle = _rule(source, ".r-graph-handle-token")
    assert handle["font-family"] == "var(--mono)"
    assert handle["font-size"] == "12px"
    assert handle["padding"] == "2px 8px"
    assert handle["border-radius"] == "4px"
    assert handle["background"] == "var(--ink)"
    assert handle["color"] == "var(--bg)"


def test_authority_list_and_canvas_columns_use_declared_dimensions() -> None:
    source = STYLESHEET.read_text(encoding="utf-8")
    component = COMPONENT.read_text(encoding="utf-8")

    authority = _rule(source, ".r-graph-authority")
    assert authority["display"] == "flex"
    assert authority["gap"] == "9px"
    assert authority["padding"] == "9px 0"
    assert authority["border-bottom"] == "1px solid var(--line)"
    assert authority["font-family"] == "var(--mono)"
    assert authority["font-size"] == "11px"

    layout = _rule(source, ".r-graph-layout")
    assert layout["grid-template-columns"] == "280px minmax(0, 1fr)"
    assert layout["gap"] == "20px"
    assert layout["margin-top"] == "16px"
    assert _rule(source, ".r-graph-members")["gap"] == "5px"
    assert _rule(source, ".r-graph-canvas-panel")["padding"] == "16px"

    assert 'className="r-graph-authority"' in component
    assert 'className="r-graph-members"' in component
    assert 'className="r-graph-canvas-panel"' in component


def test_metric_strip_and_svg_nodes_keep_canvas_typography_and_size() -> None:
    source = STYLESHEET.read_text(encoding="utf-8")
    component = COMPONENT.read_text(encoding="utf-8")

    metrics = _rule(source, ".r-graph-metrics")
    assert metrics["display"] == "flex"
    assert metrics["gap"] == "0"
    assert metrics["overflow"] == "hidden"
    metric_label = _rule(source, ".r-graph-metrics span")
    assert metric_label["font-family"] == "var(--mono)"
    assert metric_label["font-size"] == "10px"
    assert metric_label["letter-spacing"] == "0.07em"

    node = _rule(source, ".r-graph-node-card")
    assert node["position"] == "absolute"
    assert node["padding"] == "10px 12px"
    assert node["border-radius"] == "9px"
    # The stylesheet and the stage read one geometry: a card whose CSS size
    # disagrees with the layout's arithmetic overlaps its neighbour.
    geometry = evaluate_jsx_module(COMPONENT, "window.ReckonGraph.geometry")
    assert node["width"] == f"{geometry['cardWidth']}px"
    assert node["height"] == f"{geometry['cardHeight']}px"
    assert "<svg width={stage.width} height={stage.height}" in component
    assert "window.ReckonGraph.layout(members" in component
    # The surface draws no second layout of its own.
    assert "const columnGap" not in component
    assert "const rowGap" not in component


def test_the_index_rows_and_the_unnamed_controls_are_styled() -> None:
    source = STYLESHEET.read_text(encoding="utf-8")
    component = COMPONENT.read_text(encoding="utf-8")

    row = _rule(source, ".r-graph-index-row")
    assert row["display"] == "grid"
    assert row["align-items"] == "center"
    assert row["cursor"] == "pointer"
    assert _rule(source, ".r-graph-index-rows")["flex-direction"] == "column"

    # Header figures are inline mono.
    figure = _rule(source, ".r-graph-figure")
    assert figure["font-family"] == "var(--mono)"
    assert _rule(source, ".r-graph-figure b")["font"].endswith("var(--mono)")

    # An endpoint without an authored handle reads grey, not black.
    unnamed = _rule(source, ".r-graph-handle-token.unnamed")
    assert unnamed["background"] == "var(--line)"
    assert unnamed["color"] == "var(--muted)"

    # The missing-precondition chip is dashed and amber, never a ship button.
    chip = _rule(source, ".r-graph-needs-handle")
    assert chip["border"] == "1px dashed var(--warn)"
    assert chip["color"] == "var(--warn)"

    for flag in ("ready", "held", "open"):
        assert _rule(source, f".r-graph-index-flag.{flag}")["background"]

    assert 'className="r-graph-index"' in component
    assert 'className="r-graph-index-rows"' in component
    assert 'className="r-graph-needs-handle"' in component
